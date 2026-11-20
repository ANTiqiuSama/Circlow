/* circlow-keychain: reads and writes Circlow's own login-keychain items
 * (the MiMo console session, see mimo_usage.py) for the menu bar app.
 *
 * Why a separate program: since macOS 10.12 a login-keychain item also
 * carries a partition list, and an app signed without an Apple Team ID is
 * known there only by its code hash. Every update of Circlow.app changes
 * that hash, so "Always Allow" lasted only until the next build. This
 * helper does not change between updates (same source, compiler and flags,
 * signed without a timestamp), so its hash and its one "Always Allow" stay.
 *
 * It serves only a parent process signed with the same certificate as
 * itself (the "Circlow Development" identity), and only items whose service
 * starts with com.antiqiusama.circlow.
 *
 *   circlow-keychain get <service> <account>            -> the data on stdout
 *   circlow-keychain set <service> <account> [label]    <- the data on stdin
 *   circlow-keychain delete <service> <account>
 *
 * Exit status: 0 done, 2 no such item, 3 not usable here (unsigned or ad hoc
 * helper, or a caller with another signature: use the keychain directly),
 * 1 a keychain error (its OSStatus on stderr), 64 bad arguments. */
#include <CommonCrypto/CommonDigest.h>
#include <Security/Security.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

enum { DONE = 0, FAILED = 1, NOT_FOUND = 2, NOT_USABLE = 3, USAGE = 64 };
static const char PREFIX[] = "com.antiqiusama.circlow.";
static const size_t MAX_DATA = 4 << 20;

static CFStringRef text(const char *value) {
    return CFStringCreateWithCString(NULL, value, kCFStringEncodingUTF8);
}

/* "certificate leaf = H"<sha1>"" for this helper's own signing certificate;
 * NULL when it has none (unsigned or ad hoc). */
static SecRequirementRef same_certificate(void) {
    SecCodeRef self = NULL;
    SecStaticCodeRef code = NULL;
    CFDictionaryRef info = NULL;
    SecRequirementRef requirement = NULL;
    if (SecCodeCopySelf(kSecCSDefaultFlags, &self) != errSecSuccess ||
        SecCodeCopyStaticCode(self, kSecCSDefaultFlags, &code) != errSecSuccess ||
        SecCodeCopySigningInformation(code, kSecCSSigningInformation, &info) != errSecSuccess)
        goto done;
    CFArrayRef certificates = CFDictionaryGetValue(info, kSecCodeInfoCertificates);
    if (certificates == NULL || CFArrayGetCount(certificates) == 0)
        goto done;
    CFDataRef der = SecCertificateCopyData((SecCertificateRef)CFArrayGetValueAtIndex(certificates, 0));
    unsigned char digest[CC_SHA1_DIGEST_LENGTH];
    CC_SHA1(CFDataGetBytePtr(der), (CC_LONG)CFDataGetLength(der), digest);
    CFRelease(der);
    char rule[96] = "certificate leaf = H\"";
    size_t used = strlen(rule);
    for (int i = 0; i < CC_SHA1_DIGEST_LENGTH; i++)
        used += (size_t)snprintf(rule + used, sizeof rule - used, "%02x", digest[i]);
    snprintf(rule + used, sizeof rule - used, "\"");
    CFStringRef string = text(rule);
    SecRequirementCreateWithString(string, kSecCSDefaultFlags, &requirement);
    CFRelease(string);
done:
    if (info) CFRelease(info);
    if (code) CFRelease(code);
    if (self) CFRelease(self);
    return requirement;
}

/* Whether the process that started this one satisfies `requirement`. */
static int parent_satisfies(SecRequirementRef requirement) {
    int parent = (int)getppid();
    CFNumberRef pid = CFNumberCreate(NULL, kCFNumberIntType, &parent);
    const void *keys[] = {kSecGuestAttributePid}, *values[] = {pid};
    CFDictionaryRef attributes = CFDictionaryCreate(NULL, keys, values, 1, &kCFTypeDictionaryKeyCallBacks,
                                                    &kCFTypeDictionaryValueCallBacks);
    SecCodeRef code = NULL;
    OSStatus status = SecCodeCopyGuestWithAttributes(NULL, attributes, kSecCSDefaultFlags, &code);
    CFRelease(attributes);
    CFRelease(pid);
    if (status != errSecSuccess)
        return 0;
    status = SecCodeCheckValidity(code, kSecCSDefaultFlags, requirement);
    CFRelease(code);
    return status == errSecSuccess;
}

static CFDataRef read_input(void) {
    size_t size = 0, room = 4096;
    unsigned char *buffer = malloc(room);
    size_t got;
    while (buffer && (got = fread(buffer + size, 1, room - size, stdin)) > 0) {
        size += got;
        if (size > MAX_DATA) {
            free(buffer);
            return NULL;
        }
        if (size == room) {
            unsigned char *grown = realloc(buffer, room *= 2);
            if (grown == NULL) {
                free(buffer);
                return NULL;
            }
            buffer = grown;
        }
    }
    if (buffer == NULL)
        return NULL;
    CFDataRef data = CFDataCreate(NULL, buffer, (CFIndex)size);
    free(buffer);
    return data;
}

static int failed(const char *what, OSStatus status) {
    fprintf(stderr, "%s: OSStatus %d\n", what, (int)status);
    return FAILED;
}

int main(int argc, char **argv) {
    if (argc < 4 || strncmp(argv[2], PREFIX, sizeof PREFIX - 1) != 0)
        return USAGE;
    const char *verb = argv[1];
    SecRequirementRef requirement = same_certificate();
    if (requirement == NULL)
        return NOT_USABLE;
    int trusted = parent_satisfies(requirement);
    CFRelease(requirement);
    if (!trusted)
        return NOT_USABLE;

    CFMutableDictionaryRef query = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks,
                                                             &kCFTypeDictionaryValueCallBacks);
    CFStringRef service = text(argv[2]), account = text(argv[3]);
    CFDictionarySetValue(query, kSecClass, kSecClassGenericPassword);
    CFDictionarySetValue(query, kSecAttrService, service);
    CFDictionarySetValue(query, kSecAttrAccount, account);
    int result = USAGE;

    if (strcmp(verb, "get") == 0) {
        CFDictionarySetValue(query, kSecReturnData, kCFBooleanTrue);
        CFDictionarySetValue(query, kSecMatchLimit, kSecMatchLimitOne);
        CFTypeRef found = NULL;
        OSStatus status = SecItemCopyMatching(query, &found);
        if (status == errSecItemNotFound)
            result = NOT_FOUND;
        else if (status != errSecSuccess || found == NULL || CFGetTypeID(found) != CFDataGetTypeID())
            result = failed("read", status);
        else {
            CFDataRef data = (CFDataRef)found;
            size_t length = (size_t)CFDataGetLength(data);
            result = fwrite(CFDataGetBytePtr(data), 1, length, stdout) == length && fflush(stdout) == 0 ? DONE : FAILED;
        }
        if (found) CFRelease(found);
    } else if (strcmp(verb, "set") == 0) {
        CFDataRef data = read_input();
        if (data == NULL)
            result = USAGE;
        else {
            const void *keys[] = {kSecValueData}, *values[] = {data};
            CFDictionaryRef change = CFDictionaryCreate(NULL, keys, values, 1, &kCFTypeDictionaryKeyCallBacks,
                                                        &kCFTypeDictionaryValueCallBacks);
            OSStatus status = SecItemUpdate(query, change);
            CFRelease(change);
            if (status == errSecItemNotFound) {
                CFDictionarySetValue(query, kSecValueData, data);
                if (argc > 4) {
                    CFStringRef label = text(argv[4]);
                    CFDictionarySetValue(query, kSecAttrLabel, label);
                    CFRelease(label);
                }
                status = SecItemAdd(query, NULL);
            }
            result = status == errSecSuccess ? DONE : failed("write", status);
            CFRelease(data);
        }
    } else if (strcmp(verb, "delete") == 0) {
        OSStatus status = SecItemDelete(query);
        result = status == errSecSuccess || status == errSecItemNotFound ? DONE : failed("delete", status);
    }
    CFRelease(service);
    CFRelease(account);
    CFRelease(query);
    return result;
}
