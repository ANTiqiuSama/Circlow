"""The tray's once-a-second local sync (a QuotaTray mixin): live state for
the refresh scheduler, and quota readings that arrive without a network
read: Claude Code's status line, Codex's session records, the MiMo ledger;
plus the weekly ledger's bookkeeping."""
from __future__ import annotations

import itertools
import logging

import quota_sources
from quota_charts import weekly_series_name
from quota_sources import mimo_snapshot, mimo_tracker, parse_codex_session_limits
from tps_sources import performance_signature, save_performance

log = logging.getLogger("CodexQuotaTray")
PERFORMANCE_SAVE_SECONDS = 60


class LocalSync:
    """Needs from the tray: snapshots, scheduler, icons, tps_monitor,
    history, usage, weekly, last_samples, accept_snapshot(),
    announce_resets(), source_working(), active_sources()."""

    def feed_scheduler(self, performance: dict, now: float) -> None:
        """Live state for the refresh cadence: generating or a live agent of
        the source (active), and the pointer on its icon or panel (hover)."""
        moved = False
        icons = getattr(self, "icons", None) or {}
        pointed = {provider for provider, icon in icons.items()
                   if getattr(icon, "details_visible", False) or getattr(icon, "hover_open", False)}
        if pointed:
            # The panel charts every source in use, so those are being looked at too.
            pointed |= set(self.active_sources(now))
        for provider in pointed - quota_sources.WATCHED:
            quota_sources.HOVER_STARTED[provider] = now          # a new look: its first read is a real check
        quota_sources.WATCHED = frozenset(pointed)
        quota_sources.ACTIVE = frozenset(p for p in self.scheduler.providers if self.source_working(p))
        for provider in icons:
            if provider in self.scheduler.providers:
                moved |= self.scheduler.hover(provider, provider in pointed, now)
        for provider in self.scheduler.providers:
            data = performance["providers"].get(provider)
            if data is None:
                continue
            sample = data.get("last_sample_at")
            previous = self.last_samples.get(provider)
            finished = sample is not None and previous is not None and sample > previous
            if sample is not None:
                self.last_samples[provider] = sample
            elif provider not in self.last_samples:
                self.last_samples[provider] = None
            moved |= self.scheduler.activity(provider, self.source_working(provider), finished, now)
        if moved:
            self.wake.set()

    def save_performance_if_changed(self, performance: dict, now: float) -> None:
        signature = performance_signature(performance)
        if signature == self.saved_signature and now - self.saved_at < PERFORMANCE_SAVE_SECONDS:
            return
        save_performance(performance)
        self.saved_signature, self.saved_at = signature, now

    def update_claude_locally(self, now: float) -> None:
        import claude_status
        record = claude_status.latest(now)
        old = self.snapshots.get("claude")
        seen = record.get("observed_at", record["updated_at"]) if record else 0
        if record and old is not None and seen > max(old.updated_at or 0, getattr(self, "claude_rejected_seen", 0)):
            # The status line only, never /usage: this runs on the 1 s TPS loop.
            # A reading behind what is shown (another session's older figure,
            # or less than the last /usage check) is not taken.
            native = quota_sources.native_claude_quota(record)
            if not old.stale and quota_sources.claude_newer(native, old) is old:
                return
            fresh = quota_sources.keep_last_good(native, old)
            if fresh.error:
                self.claude_rejected_seen = seen
                self.accept_snapshot("claude", fresh, "status line")
                log.info("claude local quota read held: %s", fresh.error)
                return
            self.announce_resets("claude", old, fresh, now)
            if not self.accept_snapshot("claude", fresh, "status line"):
                return
            if self.history is not None and self.history.record(fresh):
                self.history.save()
            log.info("quota claude from native status: left=%s second=%s reset=%s/%s", fresh.remaining_percent,
                     fresh.secondary_remaining_percent, fresh.resets_at, fresh.secondary_resets_at)

    def update_codex_locally(self, now: float) -> None:
        """Codex writes fresh rate limits into its session record after every
        response; use them at once instead of waiting for the next App Server
        read.  A recorded "usage limit" error triggers an immediate read."""
        monitor = self.tps_monitor
        record = getattr(monitor, "codex_rate_limits", None)
        current = self.snapshots.get("codex")
        if record and current is not None and record[0] > (current.updated_at or 0) and record[0] > now - 6 * 3600:
            fresh = parse_codex_session_limits(record[1], record[0])
            if fresh is not None:
                # Session records carry no free-reset count (and older ones no balance).
                if not current.stale:
                    fresh.reset_credits = current.reset_credits
                    if fresh.credits_balance is None:
                        fresh.credits_balance = current.credits_balance
                self.announce_resets("codex", current, fresh, now)
                if not self.accept_snapshot("codex", fresh, "session record"):
                    return
                if self.history is not None and self.history.record(fresh):
                    self.history.save()
                log.info("codex quota from session record: %.0f%% left, reset %s", fresh.remaining_percent, fresh.resets_at)
                self.update_ui()
        hit = getattr(monitor, "codex_limit_hit_at", 0.0)
        if hit and hit > getattr(self, "codex_limit_seen", 0.0):
            self.codex_limit_seen = hit
            if self.scheduler is not None:
                self.scheduler.request(["codex"], now + 3, "额度用尽")
                self.wake.set()
            log.info("codex usage-limit error recorded at %s; refreshing", hit)

    def update_mimo_locally(self, now: float) -> None:
        """Fold new local MiMo token records into the Credits view (no network)."""
        tracker = mimo_tracker()
        # Only the observations added since the last second; re-reading the
        # whole day of events every second was a full pass for nothing.
        events = self.tps_monitor.events
        total = getattr(self.tps_monitor, "added_total", None)
        if total is None:
            fresh = events
        else:
            count = min(len(events), max(0, total - self.mimo_ingested))
            fresh = list(itertools.islice(reversed(events), count))[::-1] if count else []
            self.mimo_ingested = total
        added = tracker.ledger.ingest(fresh) if fresh else 0
        tracker.ledger.save()
        if added or now - self.mimo_view_at >= 60:
            self.mimo_view_at = now
            fresh = mimo_snapshot(tracker.view(now))
            if fresh.remaining_percent is not None or self.snapshots["mimo"].remaining_percent is None:
                self.accept_snapshot("mimo", fresh, "ledger")

    def note_weeks(self) -> None:
        """Record any weekly window whose week just ended (WeeklyLedger)."""
        ledger = getattr(self, "weekly", None)
        if ledger is None:
            return
        for provider, (readings, _remaining, _resets_at) in self.weekly_windows().items():
            name = weekly_series_name(self.snapshots.get(provider))
            if name and ledger.note(name, provider, readings, getattr(self, "usage", None)):
                log.info("weekly window %s: a completed week recorded", name)
