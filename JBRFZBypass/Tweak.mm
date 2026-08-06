#import <Foundation/Foundation.h>
#import "JBRFZPanel.h"

#include <dlfcn.h>
#include <dispatch/dispatch.h>
#include <mach-o/dyld.h>
#include <mach-o/loader.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdarg.h>
#include <string.h>
#include <time.h>
#include <substrate.h>

// Bridge so the panel (other TU) can cancel delayed auto actions.
static void (*gJbrfzCancelAutoActionsFn)(const char *reason) = nullptr;

extern "C" void JbrfzCancelAutoActions(const char *reason) {
    if (gJbrfzCancelAutoActionsFn != nullptr) {
        gJbrfzCancelAutoActionsFn(reason);
    }
}

namespace {

static NSString *JbrfzLocalTimestamp(void) {
    const time_t now = time(nullptr);
    struct tm localTime = {};
    if (localtime_r(&now, &localTime) == nullptr) {
        return @"<time-error>";
    }
    char buffer[40] = {};
    if (strftime(buffer, sizeof(buffer), "%Y-%m-%d %H:%M:%S %z",
                 &localTime) == 0) {
        return @"<time-error>";
    }
    return [NSString stringWithUTF8String:buffer];
}

// File log: device syslog/idevicesyslog often drops NSLog for this app.
static void JbrfzLog(NSString *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    NSString *message = [[NSString alloc] initWithFormat:fmt arguments:args];
    va_end(args);

    // High-frequency paths (e.g. monster-kill progress) must not spam disk.
    // Keep at most one identical line every 2s, and always mirror to NSLog.
    static NSString *sLastMessage = nil;
    static NSTimeInterval sLastLogTime = 0;
    const NSTimeInterval now = [[NSDate date] timeIntervalSince1970];
    if (sLastMessage != nil && [sLastMessage isEqualToString:message] &&
        (now - sLastLogTime) < 2.0) {
        return;
    }
    sLastMessage = [message copy];
    sLastLogTime = now;

    NSLog(@"%@", message);

    // Single sandbox-writable path only (triplicate writes caused disk-write jetsam).
    NSString *home = NSHomeDirectory();
    if (home.length == 0) {
        return;
    }
    NSString *path =
        [home stringByAppendingPathComponent:@"Documents/jbrfzbypass.log"];
    NSString *line = [NSString stringWithFormat:@"%@ %@\n",
                                                JbrfzLocalTimestamp(), message];
    NSData *data = [line dataUsingEncoding:NSUTF8StringEncoding];
    @try {
        NSFileManager *fm = [NSFileManager defaultManager];
        NSString *dir = [path stringByDeletingLastPathComponent];
        if (![fm fileExistsAtPath:dir]) {
            [fm createDirectoryAtPath:dir
          withIntermediateDirectories:YES
                           attributes:nil
                                error:nil];
        }
        // Rotate if oversized (~256 KB).
        NSDictionary *attrs = [fm attributesOfItemAtPath:path error:nil];
        unsigned long long size =
            [attrs[NSFileSize] unsignedLongLongValue];
        if (size > 256 * 1024) {
            [fm removeItemAtPath:path error:nil];
        }
        if (![fm fileExistsAtPath:path]) {
            [fm createFileAtPath:path contents:data attributes:nil];
            return;
        }
        NSFileHandle *fh = [NSFileHandle fileHandleForWritingAtPath:path];
        if (fh == nil) {
            return;
        }
        [fh seekToEndOfFile];
        [fh writeData:data];
        [fh closeFile];
    } @catch (__unused NSException *e) {
    }
}

using IntegrityCheck = int (*)(void);
using BoolMethod = bool (*)(void *self, void *methodInfo);
using ProgressUpdatedMethod = void (*)(void *self,
                                      void *modifiedRecords,
                                      void *completedRecords,
                                      void *methodInfo);
using UpdateUIMethod = void (*)(void *self, void *methodInfo);
using RecordBoolMethod = bool (*)(void *self, void *methodInfo);
using GuideClickMethod = void (*)(void *self, void *methodInfo);
using LoadingSetMethod = void *(*)(void *self, void *key, void *methodInfo);
using LoadingUnsetMethod = void (*)(void *self, void *key, void *methodInfo);
static GuideClickMethod gOriginalHandleOnGuideUIClick = nullptr;
static LoadingSetMethod gOriginalLoadingFlagSet = nullptr;
static atomic_uintptr_t gLoadingFlagInstance = 0;
static atomic_uintptr_t gLastLoadingKey = 0;

// ---- Invite / stage / account capture (for local Python automation) ----
using ExchangeBeforeRequestMethod = void (*)(void *self, int exchangeId,
                                             void *headers, void *method,
                                             void *request, int64_t requestTime,
                                             int options, void *methodInfo);
using ExchangeAfterResponseOkMethod = void (*)(void *self, int exchangeId,
                                               void *method, void *response,
                                               void *metadata, int options,
                                               void *methodInfo);
using ExchangeAfterResponseErrMethod = void (*)(void *self, int exchangeId,
                                                void *method, void *exception,
                                                uint64_t guidLo, uint64_t guidHi,
                                                int options, void *methodInfo);
using InviteLinkMethod = void *(*)(void *self, void *mid, void *methodInfo);
using GuestSetMethod = void (*)(void *loginInfo, void *methodInfo);
using ProcessLoginMethod = int (*)(void *response, void *methodInfo);
using GrpcChannelCtorMethod = void (*)(void *self, void *endpoint, void *methodInfo);
using EndpointToStringMethod = void *(*)(void *endpoint, void *methodInfo);
using CreateHeadersMethod = void *(*)(void *self, uint64_t guidLo, uint64_t guidHi,
                                      int64_t requestTime, int options,
                                      void *methodInfo);
using CreateChannelMethod = void *(*)(void *endpoint, void *methodInfo);

static ExchangeBeforeRequestMethod gOriginalExchangeBeforeRequest = nullptr;
static ExchangeAfterResponseOkMethod gOriginalExchangeAfterOk = nullptr;
static ExchangeAfterResponseErrMethod gOriginalExchangeAfterErr = nullptr;
static InviteLinkMethod gOriginalCreateFriendInvitationLink = nullptr;
static GuestSetMethod gOriginalGuestLoginKeyChainSet = nullptr;
static ProcessLoginMethod gOriginalProcessLoginResponse = nullptr;
static GrpcChannelCtorMethod gOriginalGrpcChannelCtor = nullptr;
static EndpointToStringMethod gOriginalEndpointToString = nullptr;
static CreateHeadersMethod gOriginalCreateHeaders = nullptr;
static CreateChannelMethod gOriginalCreateChannel = nullptr;
static atomic_int gHeaderCaptureCount = 0;
static atomic_int gEndpointCaptureCount = 0;

// Stage/invite direct hooks (kept alongside the active main-scene RPC listener).
// UniTask return is treated as opaque bytes for pass-through.
struct JbrfzUniTaskShim {
    uint64_t words[4];
};
using StartStageMethod = JbrfzUniTaskShim (*)(void *self, int stageIndex,
                                              int battleTeamId, int startPoint,
                                              int startTrigger,
                                              void *stageBattleReport,
                                              void *methodInfo);
using CompleteStageMethod = JbrfzUniTaskShim (*)(void *self, int stageIndex,
                                                 int startPoint,
                                                 void *stageClearReport,
                                                 void *clientBattleReport,
                                                 void *methodInfo);
using RegisterFriendInviterMethod = JbrfzUniTaskShim (*)(void *self,
                                                         void *inviterId,
                                                         void *methodInfo);
static StartStageMethod gOriginalStartStageAsync = nullptr;
static CompleteStageMethod gOriginalCompleteStageAsync = nullptr;
static RegisterFriendInviterMethod gOriginalRegisterFriendInviter = nullptr;


using OvenCtorMethod = void (*)(void *self, void *equipmentApi, void *transactor,
                                void *equipmentTable, void *combatPowerCalculator,
                                void *gameSettingTable, void *ovenRecord,
                                void *equipmentPresetRecordSet, void *totalItemRecord,
                                void *ovenLevelUpTable, void *transactionNotifier,
                                void *methodInfo);
using PageOpenMethod = void (*)(void *self, void *methodInfo);
// Cookie/Pet gacha 十连: HandleOnClickPurchase(buttonIndex)
// buttons are 0=单抽, 1=十连, 2=30连.
using GachaPurchaseMethod = void (*)(void *self, int buttonIndex, void *methodInfo);
// Oven guide action uses OvenAutoDrawService.StartAuto(preset, nonstop).
// Cookie/Pet gacha guide action uses HandleOnClickPurchase(buttonIndex=1).

static atomic_bool gInstalled = false;
static atomic_bool gNormalStateScheduled = false;
static atomic_uintptr_t gMainExecutableBase = 0;
static atomic_uintptr_t gUnityFrameworkBase = 0;
static IntegrityCheck gOriginalAbnormalEnvironment = nullptr;
static IntegrityCheck gOriginalSwizzling = nullptr;
static IntegrityCheck gOriginalSwizzlingIter = nullptr;
static void *gOriginalStartRoutine = nullptr;
static void *gOriginalAppSealingRoutine = nullptr;
static void *gOriginalExitFunction = nullptr;
static void *gOriginalRuntimeLoad = nullptr;
static void *gOriginalProtectionThreads[20] = {};
static BoolMethod gOriginalIsAdRemoveActive = nullptr;
static void *gOriginalAfterError = nullptr;
static void *gOriginalAfterResponseError = nullptr;
static ProgressUpdatedMethod gOriginalHandleOnProgressUpdated =
    nullptr;
static UpdateUIMethod gOriginalUpdateUI = nullptr;
// Requirement residual-record fix (kill-2000 false complete).
using RequirementRegisterMethod =
    bool (*)(void *self, unsigned int id, void *values);
using RequirementCalculateCurrentValueMethod =
    int64_t (*)(void *self, int64_t type, void *key);
static RequirementRegisterMethod gOriginalRequirementRegister = nullptr;
static RequirementCalculateCurrentValueMethod
    gOriginalCalculateCurrentValue = nullptr;
static atomic_int gRequirementStaleCleanupCount = 0;
static OvenCtorMethod gOriginalOvenAutoDrawCtor = nullptr;
static PageOpenMethod gOriginalCookieGachaOnPageOpen = nullptr;
static PageOpenMethod gOriginalPetGachaOnPageOpen = nullptr;
static PageOpenMethod gOriginalInventoryOnPopupOpen = nullptr;
static PageOpenMethod gOriginalInventoryItemInfoOnPopupOpen = nullptr;
static atomic_uintptr_t gUnityBaseForGuide = 0;
static atomic_uintptr_t gOvenAutoDrawService = 0;
static atomic_uintptr_t gCookieGachaPresenter = 0;
static atomic_uintptr_t gPetGachaPresenter = 0;
static atomic_uintptr_t gInventoryPresenter = 0;
static atomic_uintptr_t gInventoryItemInfoPresenter = 0;
static atomic_uintptr_t gGuidePresenter = 0;
// When set, inventory open / item-info open will continue the open-box chain.
static atomic_bool gPendingUseRandomRewardBox = false;
// Bumped to invalidate delayed auto-action blocks (manual ops / guide switch).
static atomic_int gAutoActionGeneration = 0;
static atomic_int gLastSkipClaimGuideId = 0;

static int ReturnNormalEnvironment(void) {
    return 0;
}

static void ReturnWithoutAction(void) {
}

// ===================== Capture helpers (invite / stage / account) =====================
// Writes Documents/jbrfz_capture.log + optional *.bin for key protobuf bodies.
// Goal: collect guest secret, tokens, mid, invite link, Start/CompleteStage payloads,
// RegisterFriendInviter, SignUp, and game endpoint for a local Python bot.

static void JbrfzCaptureLog(NSString *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    NSString *message = [[NSString alloc] initWithFormat:fmt arguments:args];
    va_end(args);

    NSLog(@"%@", message);

    NSString *home = NSHomeDirectory();
    if (home.length == 0) {
        return;
    }
    NSString *path =
        [home stringByAppendingPathComponent:@"Documents/jbrfz_capture.log"];
    NSString *line = [NSString stringWithFormat:@"%@ %@\n",
                                                JbrfzLocalTimestamp(), message];
    NSData *data = [line dataUsingEncoding:NSUTF8StringEncoding];
    @try {
        NSFileManager *fm = [NSFileManager defaultManager];
        NSString *dir = [path stringByDeletingLastPathComponent];
        if (![fm fileExistsAtPath:dir]) {
            [fm createDirectoryAtPath:dir
          withIntermediateDirectories:YES
                           attributes:nil
                                error:nil];
        }
        NSDictionary *attrs = [fm attributesOfItemAtPath:path error:nil];
        unsigned long long size =
            [attrs[NSFileSize] unsignedLongLongValue];
        // Capture log can be large; rotate at ~2 MB.
        if (size > 2 * 1024 * 1024) {
            [fm removeItemAtPath:path error:nil];
        }
        if (![fm fileExistsAtPath:path]) {
            [fm createFileAtPath:path contents:data attributes:nil];
            return;
        }
        NSFileHandle *fh = [NSFileHandle fileHandleForWritingAtPath:path];
        if (fh == nil) {
            return;
        }
        [fh seekToEndOfFile];
        [fh writeData:data];
        [fh closeFile];
    } @catch (__unused NSException *e) {
    }
}

static NSString *Il2CppStringToNSString(void *str) {
    if (str == nullptr) {
        return nil;
    }
    // Il2CppString: length @ +0x10 (int32), chars @ +0x14 (utf16)
    const int32_t length =
        *reinterpret_cast<const int32_t *>(reinterpret_cast<const uint8_t *>(str) +
                                           0x10);
    if (length <= 0 || length > 1 << 20) {
        return nil;
    }
    const unichar *chars = reinterpret_cast<const unichar *>(
        reinterpret_cast<const uint8_t *>(str) + 0x14);
    return [NSString stringWithCharacters:chars length:static_cast<NSUInteger>(length)];
}

static NSString *MethodFullName(void *methodObj) {
    if (methodObj == nullptr) {
        return @"<null-method>";
    }
    // Prefer Grpc.Core.Method`2 field layout:
    // +0x18 serviceName, +0x20 name, +0x38 fullName
    auto *bytes = reinterpret_cast<uint8_t *>(methodObj);
    NSString *full = Il2CppStringToNSString(*reinterpret_cast<void **>(bytes + 0x38));
    if (full.length > 0) {
        return full;
    }
    NSString *service =
        Il2CppStringToNSString(*reinterpret_cast<void **>(bytes + 0x18));
    NSString *name =
        Il2CppStringToNSString(*reinterpret_cast<void **>(bytes + 0x20));
    if (service.length > 0 || name.length > 0) {
        return [NSString stringWithFormat:@"%@/%@", service ?: @"?",
                                         name ?: @"?"];
    }
    // Fallback: game helper uses IMethod virtual FullName.
    uintptr_t base = atomic_load(&gUnityBaseForGuide);
    if (base == 0) {
        base = atomic_load(&gUnityFrameworkBase);
    }
    if (base != 0) {
        auto getRpcName =
            reinterpret_cast<void *(*)(void *, void *)>(base + 0x03DAE3B0);
        void *str = nullptr;
        @try {
            str = getRpcName(methodObj, nullptr);
        } @catch (__unused NSException *e) {
            str = nullptr;
        }
        NSString *via = Il2CppStringToNSString(str);
        if (via.length > 0) {
            return via;
        }
    }
    return [NSString stringWithFormat:@"<method %p>", methodObj];
}

static bool IsCaptureInterestingMethod(NSString *fullName) {
    // During collection, keep almost everything; only drop pure noise.
    if (fullName.length == 0) {
        return false;
    }
    NSString *lower = [fullName lowercaseString];
    if ([lower containsString:@"ping"] || [lower containsString:@"heartbeat"]) {
        return false;
    }
    return true;
}

static bool IsCaptureHighValueMethod(NSString *fullName) {
    if (fullName.length == 0) {
        return false;
    }
    NSString *lower = [fullName lowercaseString];
    NSArray<NSString *> *keys = @[
        @"completestage", @"startstage", @"registerfriendinviter", @"signup",
        @"getguestsecret", @"setguestsecret", @"adventure", @"friend",
        @"invite", @"stage", @"guild"
    ];
    for (NSString *k in keys) {
        if ([lower containsString:k]) {
            return true;
        }
    }
    return false;
}

static NSString *MessageToDiagnosticString(void *message) {
    if (message == nullptr) {
        return nil;
    }
    uintptr_t base = atomic_load(&gUnityBaseForGuide);
    if (base == 0) {
        base = atomic_load(&gUnityFrameworkBase);
    }
    if (base == 0) {
        return nil;
    }
    // Google.Protobuf.JsonFormatter.ToDiagnosticString(IMessage)
    auto toDiag = reinterpret_cast<void *(*)(void *, void *)>(base + 0x0A9B1494);
    void *str = nullptr;
    @try {
        str = toDiag(message, nullptr);
    } @catch (__unused NSException *e) {
        return nil;
    }
    return Il2CppStringToNSString(str);
}

static NSData *MessageToNSData(void *message) {
    if (message == nullptr) {
        return nil;
    }
    uintptr_t base = atomic_load(&gUnityBaseForGuide);
    if (base == 0) {
        base = atomic_load(&gUnityFrameworkBase);
    }
    if (base == 0) {
        return nil;
    }
    // Google.Protobuf.MessageExtensions.ToByteArray(IMessage)
    auto toBytes =
        reinterpret_cast<void *(*)(void *, void *)>(base + 0x0A9B7D70);
    void *arr = nullptr;
    @try {
        arr = toBytes(message, nullptr);
    } @catch (__unused NSException *e) {
        return nil;
    }
    if (arr == nullptr) {
        return nil;
    }
    // Il2CppArray: max_length @ +0x18, data @ +0x20
    const int32_t len =
        *reinterpret_cast<const int32_t *>(reinterpret_cast<const uint8_t *>(arr) +
                                           0x18);
    if (len <= 0 || len > 8 * 1024 * 1024) {
        return nil;
    }
    const void *data = reinterpret_cast<const uint8_t *>(arr) + 0x20;
    return [NSData dataWithBytes:data length:static_cast<NSUInteger>(len)];
}

static void WriteCaptureBinary(NSString *name, NSData *data) {
    if (name.length == 0 || data.length == 0) {
        return;
    }
    NSString *home = NSHomeDirectory();
    if (home.length == 0) {
        return;
    }
    NSString *dir =
        [home stringByAppendingPathComponent:@"Documents/jbrfz_capture"];
    @try {
        NSFileManager *fm = [NSFileManager defaultManager];
        if (![fm fileExistsAtPath:dir]) {
            [fm createDirectoryAtPath:dir
          withIntermediateDirectories:YES
                           attributes:nil
                                error:nil];
        }
        NSString *path = [dir stringByAppendingPathComponent:name];
        [data writeToFile:path atomically:YES];
        JbrfzCaptureLog(@"[CAPTURE] wrote binary %@ (%lu bytes)", name,
                        (unsigned long)data.length);
    } @catch (__unused NSException *e) {
    }
}

static NSString *MetadataToString(void *metadata) {
    if (metadata == nullptr) {
        return @"<null-metadata>";
    }
    // Metadata.entries List @ +0x10
    void *list = *reinterpret_cast<void **>(
        reinterpret_cast<uint8_t *>(metadata) + 0x10);
    if (list == nullptr) {
        return @"<empty-metadata>";
    }
    void *items = *reinterpret_cast<void **>(
        reinterpret_cast<uint8_t *>(list) + 0x10);
    const int size =
        *reinterpret_cast<int *>(reinterpret_cast<uint8_t *>(list) + 0x18);
    if (items == nullptr || size <= 0) {
        return @"<empty-metadata>";
    }
    NSMutableString *out = [NSMutableString string];
    const int n = size > 32 ? 32 : size;
    for (int i = 0; i < n; i++) {
        void *entry = *reinterpret_cast<void **>(
            reinterpret_cast<uint8_t *>(items) + 0x20 +
            static_cast<size_t>(i) * sizeof(void *));
        if (entry == nullptr) {
            continue;
        }
        void *keyPtr = *reinterpret_cast<void **>(
            reinterpret_cast<uint8_t *>(entry) + 0x10);
        void *valPtr = *reinterpret_cast<void **>(
            reinterpret_cast<uint8_t *>(entry) + 0x18);
        NSString *key = Il2CppStringToNSString(keyPtr) ?: @"?";
        NSString *val = Il2CppStringToNSString(valPtr) ?: @"<bin/null>";
        // redact nothing for capture phase; tokens are required for offline bot.
        if (out.length > 0) {
            [out appendString:@"; "];
        }
        [out appendFormat:@"%@=%@", key, val];
    }
    if (size > n) {
        [out appendFormat:@"; ...(+%d)", size - n];
    }
    return out;
}

static void CaptureExchange(const char *phase, int exchangeId, void *method,
                            void *message, void *headersOrMeta) {
    NSString *fullName = MethodFullName(method);
    if (!IsCaptureInterestingMethod(fullName)) {
        return;
    }
    const bool high = IsCaptureHighValueMethod(fullName);
    NSString *meta = MetadataToString(headersOrMeta);
    NSString *body = nil;
    @try {
        body = MessageToDiagnosticString(message);
    } @catch (__unused NSException *e) {
        body = @"<diag-exception>";
    }
    if (body.length > 8000) {
        body = [[body substringToIndex:8000] stringByAppendingString:@"...<trunc>"];
    }
    // Always write a compact line so we can see traffic volume.
    JbrfzCaptureLog(@"[CAPTURE][%s] id=%d method=%@ msg=%p meta={%@} body=%@",
                    phase, exchangeId, fullName, message, meta,
                    body ?: @"<no-body>");

    if (high && message != nullptr) {
        NSData *bin = nil;
        @try {
            bin = MessageToNSData(message);
        } @catch (__unused NSException *e) {
            bin = nil;
        }
        if (bin.length > 0) {
            NSString *safe = [[fullName
                stringByReplacingOccurrencesOfString:@"/"
                                          withString:@"_"]
                stringByReplacingOccurrencesOfString:@" "
                                          withString:@"_"];
            if (safe.length == 0) {
                safe = @"rpc";
            }
            if (safe.length > 80) {
                safe = [safe substringFromIndex:safe.length - 80];
            }
            NSString *fname =
                [NSString stringWithFormat:@"%s_%d_%@.bin", phase, exchangeId,
                                           safe];
            WriteCaptureBinary(fname, bin);
        }
    }
}

static void HookedExchangeBeforeRequest(void *self, int exchangeId,
                                        void *headers, void *method,
                                        void *request, int64_t requestTime,
                                        int options, void *methodInfo) {
    // Always emit a raw line first so we can tell if this hook fires at all.
    JbrfzCaptureLog(@"[CAPTURE][REQ-RAW] id=%d method=%p req=%p headers=%p",
                    exchangeId, method, request, headers);
    CaptureExchange("REQ", exchangeId, method, request, headers);
    if (gOriginalExchangeBeforeRequest != nullptr) {
        gOriginalExchangeBeforeRequest(self, exchangeId, headers, method,
                                       request, requestTime, options,
                                       methodInfo);
    }
}

static void HookedExchangeAfterOk(void *self, int exchangeId, void *method,
                                  void *response, void *metadata, int options,
                                  void *methodInfo) {
    JbrfzCaptureLog(@"[CAPTURE][OK-RAW] id=%d method=%p resp=%p", exchangeId,
                    method, response);
    CaptureExchange("OK", exchangeId, method, response, metadata);
    if (gOriginalExchangeAfterOk != nullptr) {
        gOriginalExchangeAfterOk(self, exchangeId, method, response, metadata,
                                 options, methodInfo);
    }
}

static void HookedExchangeAfterErr(void *self, int exchangeId, void *method,
                                   void *exception, uint64_t guidLo,
                                   uint64_t guidHi, int options,
                                   void *methodInfo) {
    NSString *fullName = MethodFullName(method);
    if (IsCaptureInterestingMethod(fullName)) {
        JbrfzCaptureLog(@"[CAPTURE][ERR] id=%d method=%@ exception=%p",
                        exchangeId, fullName, exception);
    }
    if (gOriginalExchangeAfterErr != nullptr) {
        gOriginalExchangeAfterErr(self, exchangeId, method, exception, guidLo,
                                  guidHi, options, methodInfo);
    }
}

static void *HookedCreateFriendInvitationLink(void *self, void *mid,
                                              void *methodInfo) {
    void *result = nullptr;
    if (gOriginalCreateFriendInvitationLink != nullptr) {
        result = gOriginalCreateFriendInvitationLink(self, mid, methodInfo);
    }
    NSString *midStr = Il2CppStringToNSString(mid);
    // Mid is struct{string}; when passed by value on arm64 it is the string ptr.
    if (midStr == nil && mid != nullptr) {
        // try as Mid* / boxed
        void *inner = *reinterpret_cast<void **>(mid);
        midStr = Il2CppStringToNSString(inner);
    }
    NSString *link = Il2CppStringToNSString(result);
    JbrfzCaptureLog(@"[CAPTURE][INVITE_LINK] mid=%@ link=%@",
                    midStr ?: @"?", link ?: @"?");
    return result;
}

static void HookedGuestLoginKeyChainSet(void *loginInfo, void *methodInfo) {
    if (loginInfo != nullptr) {
        void *midPtr = *reinterpret_cast<void **>(
            reinterpret_cast<uint8_t *>(loginInfo) + 0x10);
        void *secretPtr = *reinterpret_cast<void **>(
            reinterpret_cast<uint8_t *>(loginInfo) + 0x18);
        JbrfzCaptureLog(@"[CAPTURE][GUEST_SECRET] mid=%@ secret=%@",
                        Il2CppStringToNSString(midPtr) ?: @"?",
                        Il2CppStringToNSString(secretPtr) ?: @"?");
    }
    if (gOriginalGuestLoginKeyChainSet != nullptr) {
        gOriginalGuestLoginKeyChainSet(loginInfo, methodInfo);
    }
}

static int HookedProcessLoginResponse(void *response, void *methodInfo) {
    if (response != nullptr) {
        auto readField = [](void *obj, size_t off) -> NSString * {
            void *p = *reinterpret_cast<void **>(
                reinterpret_cast<uint8_t *>(obj) + off);
            return Il2CppStringToNSString(p);
        };
        // LoginResponse fields (see DevPlaySDK LoginResponse.cs)
        JbrfzCaptureLog(
            @"[CAPTURE][LOGIN] refresh=%@ game_access=%@ oven_access=%@ "
            @"guest_secret=%@ token=%@ login_type=%@ device_secret=%@ "
            @"is_join=%d",
            readField(response, 0x30), readField(response, 0x38),
            readField(response, 0x40), readField(response, 0x58),
            readField(response, 0x80), readField(response, 0x78),
            readField(response, 0xC0),
            *reinterpret_cast<bool *>(reinterpret_cast<uint8_t *>(response) +
                                      0x70)
                ? 1
                : 0);
        void *member = *reinterpret_cast<void **>(
            reinterpret_cast<uint8_t *>(response) + 0x48);
        if (member != nullptr) {
            // best-effort: dump first few string-looking fields
            JbrfzCaptureLog(@"[CAPTURE][LOGIN_MEMBER] ptr=%p f10=%@ f18=%@ f20=%@",
                            member, readField(member, 0x10),
                            readField(member, 0x18), readField(member, 0x20));
        }
    }
    if (gOriginalProcessLoginResponse != nullptr) {
        return gOriginalProcessLoginResponse(response, methodInfo);
    }
    return 0;
}

static void HookedGrpcChannelCtor(void *self, void *endpoint, void *methodInfo) {
    if (gOriginalGrpcChannelCtor != nullptr) {
        gOriginalGrpcChannelCtor(self, endpoint, methodInfo);
    }
    // Endpoint struct: protocol@0 domain@8 port@16 (by ref in X1)
    if (endpoint != nullptr) {
        void *proto = *reinterpret_cast<void **>(endpoint);
        void *domain =
            *reinterpret_cast<void **>(reinterpret_cast<uint8_t *>(endpoint) +
                                       0x8);
        int port =
            *reinterpret_cast<int *>(reinterpret_cast<uint8_t *>(endpoint) +
                                     0x10);
        JbrfzCaptureLog(@"[CAPTURE][ENDPOINT] %@://%@:%d",
                        Il2CppStringToNSString(proto) ?: @"?",
                        Il2CppStringToNSString(domain) ?: @"?", port);
    }
    if (self != nullptr) {
        // GameEndpoint copy lives at provider+0x10
        void *proto = *reinterpret_cast<void **>(
            reinterpret_cast<uint8_t *>(self) + 0x10);
        void *domain = *reinterpret_cast<void **>(
            reinterpret_cast<uint8_t *>(self) + 0x18);
        int port = *reinterpret_cast<int *>(
            reinterpret_cast<uint8_t *>(self) + 0x20);
        JbrfzCaptureLog(@"[CAPTURE][ENDPOINT_STORED] %@://%@:%d",
                        Il2CppStringToNSString(proto) ?: @"?",
                        Il2CppStringToNSString(domain) ?: @"?", port);
    }
}

static void *HookedEndpointToString(void *endpoint, void *methodInfo) {
    void *result = nullptr;
    if (gOriginalEndpointToString != nullptr) {
        result = gOriginalEndpointToString(endpoint, methodInfo);
    }
    const int n = atomic_fetch_add(&gEndpointCaptureCount, 1);
    if (n < 20) {
        JbrfzCaptureLog(@"[CAPTURE][ENDPOINT_URL] %@",
                        Il2CppStringToNSString(result) ?: @"?");
    }
    return result;
}

static void *HookedCreateChannel(void *endpoint, void *methodInfo) {
    if (endpoint != nullptr) {
        void *proto = *reinterpret_cast<void **>(endpoint);
        void *domain =
            *reinterpret_cast<void **>(reinterpret_cast<uint8_t *>(endpoint) +
                                       0x8);
        int port =
            *reinterpret_cast<int *>(reinterpret_cast<uint8_t *>(endpoint) +
                                     0x10);
        JbrfzCaptureLog(@"[CAPTURE][CREATE_CHANNEL] %@://%@:%d",
                        Il2CppStringToNSString(proto) ?: @"?",
                        Il2CppStringToNSString(domain) ?: @"?", port);
    }
    if (gOriginalCreateChannel != nullptr) {
        return gOriginalCreateChannel(endpoint, methodInfo);
    }
    return nullptr;
}

static void *HookedCreateHeaders(void *self, uint64_t guidLo, uint64_t guidHi,
                                 int64_t requestTime, int options,
                                 void *methodInfo) {
    void *meta = nullptr;
    if (gOriginalCreateHeaders != nullptr) {
        meta = gOriginalCreateHeaders(self, guidLo, guidHi, requestTime, options,
                                      methodInfo);
    }
    const int n = atomic_fetch_add(&gHeaderCaptureCount, 1);
    // first few full dumps are enough for offline bot header map
    if (n < 8) {
        JbrfzCaptureLog(@"[CAPTURE][HEADERS] n=%d options=%d meta={%@}", n, options,
                        MetadataToString(meta));
    }
    return meta;
}


static void LogProtobufMessage(const char *tag, void *message) {
    if (message == nullptr) {
        JbrfzCaptureLog(@"[CAPTURE][%s] msg=<null>", tag);
        return;
    }
    NSString *body = nil;
    @try {
        body = MessageToDiagnosticString(message);
    } @catch (__unused NSException *e) {
        body = @"<diag-exception>";
    }
    if (body.length > 12000) {
        body = [[body substringToIndex:12000] stringByAppendingString:@"...<trunc>"];
    }
    JbrfzCaptureLog(@"[CAPTURE][%s] ptr=%p body=%@", tag, message,
                    body ?: @"<no-body>");
    NSData *bin = nil;
    @try {
        bin = MessageToNSData(message);
    } @catch (__unused NSException *e) {
        bin = nil;
    }
    if (bin.length > 0) {
        NSString *fname =
            [NSString stringWithFormat:@"%s_%lu.bin", tag,
                                      (unsigned long)bin.length];
        WriteCaptureBinary(fname, bin);
    }
}

static JbrfzUniTaskShim HookedStartStageAsync(void *self, int stageIndex,
                                              int battleTeamId, int startPoint,
                                              int startTrigger,
                                              void *stageBattleReport,
                                              void *methodInfo) {
    JbrfzCaptureLog(
        @"[CAPTURE][StartStage] stage=%d team=%d startPoint=%d trigger=%d report=%p",
        stageIndex, battleTeamId, startPoint, startTrigger, stageBattleReport);
    LogProtobufMessage("StartStageReport", stageBattleReport);
    if (gOriginalStartStageAsync != nullptr) {
        return gOriginalStartStageAsync(self, stageIndex, battleTeamId,
                                        startPoint, startTrigger,
                                        stageBattleReport, methodInfo);
    }
    return JbrfzUniTaskShim{};
}

static JbrfzUniTaskShim HookedCompleteStageAsync(void *self, int stageIndex,
                                                 int startPoint,
                                                 void *stageClearReport,
                                                 void *clientBattleReport,
                                                 void *methodInfo) {
    JbrfzCaptureLog(
        @"[CAPTURE][CompleteStage] stage=%d startPoint=%d clear=%p client=%p",
        stageIndex, startPoint, stageClearReport, clientBattleReport);
    LogProtobufMessage("StageClearReport", stageClearReport);
    LogProtobufMessage("ClientBattleReport", clientBattleReport);
    if (gOriginalCompleteStageAsync != nullptr) {
        return gOriginalCompleteStageAsync(self, stageIndex, startPoint,
                                           stageClearReport, clientBattleReport,
                                           methodInfo);
    }
    return JbrfzUniTaskShim{};
}

static JbrfzUniTaskShim HookedRegisterFriendInviter(void *self, void *inviterId,
                                                    void *methodInfo) {
    NSString *mid = Il2CppStringToNSString(inviterId);
    JbrfzCaptureLog(@"[CAPTURE][RegisterFriendInviter] inviter=%@",
                    mid ?: @"?");
    if (gOriginalRegisterFriendInviter != nullptr) {
        return gOriginalRegisterFriendInviter(self, inviterId, methodInfo);
    }
    return JbrfzUniTaskShim{};
}

// Invoke IL2CPP Action<bool> using the same layout the game uses:
// fn = *(action+0x18), target = *(action+0x40), method = *(action+0x28)
static void InvokeBoolAction(void *action, int value) {
    if (action == nullptr) {
        return;
    }
    auto *bytes = reinterpret_cast<uint8_t *>(action);
    auto fn = *reinterpret_cast<void (**)(void *, int, void *)>(bytes + 0x18);
    void *target = *reinterpret_cast<void **>(bytes + 0x40);
    void *method = *reinterpret_cast<void **>(bytes + 0x28);
    if (fn != nullptr) {
        fn(target, value, method);
    }
}

// Force-clear LoadingFlag regardless of exchangeId key matching.
// Original AfterResponse Unsets with a newly boxed ExchangeId; if our cached
// key mismatches, Unset no-ops and the fullscreen loader stays forever.
static void ForceClearLoadingFlag(void *loadingFlag, const char *reason) {
    if (loadingFlag == nullptr) {
        return;
    }
    uintptr_t base = atomic_load(&gUnityBaseForGuide);
    if (base == 0) {
        base = atomic_load(&gUnityFrameworkBase);
    }

    // Prefer precise Unset with last Set key (matches LoadingFlag.Unset path).
    void *key = reinterpret_cast<void *>(atomic_load(&gLastLoadingKey));
    if (key != nullptr && base != 0) {
        auto unset =
            reinterpret_cast<LoadingUnsetMethod>(base + 0x03BB0D3C);
        unset(loadingFlag, key, nullptr);
    }

    // Always force ObjectSetFlag.Clear + LoadingFlag.OnFlagChanged(false).
    // LoadingFlag._flag (ObjectSetFlag) @ +0x10
    // LoadingFlag.OnFlagChanged          @ +0x18
    void *objectSet =
        *reinterpret_cast<void **>(reinterpret_cast<uint8_t *>(loadingFlag) +
                                   0x10);
    if (objectSet != nullptr && base != 0) {
        // ObjectSetFlag.Clear @ 0x03DBAA58 — empties keys and hides if active.
        auto clear =
            reinterpret_cast<void (*)(void *, void *)>(base + 0x03DBAA58);
        clear(objectSet, nullptr);
    }
    void *onChanged =
        *reinterpret_cast<void **>(reinterpret_cast<uint8_t *>(loadingFlag) +
                                   0x18);
    InvokeBoolAction(onChanged, 0);

    atomic_store(&gLastLoadingKey, 0);
    JbrfzLog(@"[JBRFZBypass] ForceClear LoadingFlag (%s) flag=%p keyWas=%p",
             reason ? reason : "rpc-error", loadingFlag, key);
}

// Clear fullscreen LoadingFlag and GuidePresenter wait bit without showing
// the business-error popup / AfterError restart path.
static void DismissLoadingAndGuideWait(const char *reason) {
    void *flag = reinterpret_cast<void *>(atomic_load(&gLoadingFlagInstance));
    ForceClearLoadingFlag(flag, reason);

    void *guide =
        reinterpret_cast<void *>(atomic_load(&gGuidePresenter));
    if (guide != nullptr) {
        // GuidePresenter._isWaitingForResponse @ +0x80
        reinterpret_cast<uint8_t *>(guide)[0x80] = 0;
    }
    JbrfzLog(@"[JBRFZBypass] Dismiss loading/wait (%s)",
             reason ? reason : "rpc-error");
}

// Capture loading keys so RPC-error path can Unset without opening popup.
static void *HookedLoadingFlagSet(void *self, void *key, void *methodInfo) {
    if (self != nullptr) {
        atomic_store(&gLoadingFlagInstance, reinterpret_cast<uintptr_t>(self));
    }
    if (key != nullptr) {
        atomic_store(&gLastLoadingKey, reinterpret_cast<uintptr_t>(key));
    }
    if (gOriginalLoadingFlagSet != nullptr) {
        return gOriginalLoadingFlagSet(self, key, methodInfo);
    }
    return nullptr;
}

// AfterResponse(RpcException): keep Unset(loading) + clear wait; skip popup.
// ABI (arm64): self, exchangeId, method, exception, guid_lo, guid_hi, options, methodInfo
// LoadingFlag lives at MainSceneExchangeEventListener +0x20.
static void HookedAfterResponseRpcException(void *self, int exchangeId,
                                            void *method, void *exception,
                                            uint64_t guidLo, uint64_t guidHi,
                                            int options, void *methodInfo) {
    (void)guidLo;
    (void)guidHi;
    (void)methodInfo;

    JbrfzCaptureLog(@"[CAPTURE][ERR] id=%d method=%@ exception=%p options=%d",
                    exchangeId, MethodFullName(method), exception, options);

    void *flag = nullptr;
    if (self != nullptr) {
        flag = *reinterpret_cast<void **>(reinterpret_cast<uint8_t *>(self) +
                                          0x20);
        if (flag != nullptr) {
            atomic_store(&gLoadingFlagInstance,
                         reinterpret_cast<uintptr_t>(flag));
        }
    }
    ForceClearLoadingFlag(flag, "AfterResponse(RpcException)");

    void *guide =
        reinterpret_cast<void *>(atomic_load(&gGuidePresenter));
    if (guide != nullptr) {
        reinterpret_cast<uint8_t *>(guide)[0x80] = 0;
    }
    JbrfzLog(@"[JBRFZBypass] Dismiss loading/wait (AfterResponse(RpcException) "
             "exchangeId=%d flag=%p)",
             exchangeId, flag);
}

// AfterError: never restart/login; still clear loading/wait if still stuck.
static void HookedAfterError(void *self, void *info, void *methodInfo) {
    (void)self;
    (void)info;
    (void)methodInfo;
    DismissLoadingAndGuideWait("AfterError");
}


static void *ReturnNullThread(void *argument) {
    (void)argument;
    return nullptr;
}

// Crumble.AdRemoveGameBoostCalculator.IsAdRemoveActive /
// AdService.IsRemovedAd. Force the client into the "ad free card"
// path so rewarded ads short-circuit as success without playback.
static bool ReturnAdRemoveActive(void *self, void *methodInfo) {
    (void)self;
    (void)methodInfo;
    return true;
}

// ---------------------------------------------------------------------------
// Navigation guide automation
// ---------------------------------------------------------------------------
// GuidePresenter.HandleOnGuideUIClick @ 0x04215764:
//   completed -> ClaimGuideRewardAsync().Forget()
//   incomplete -> OpenShortcutAsync().Forget()
// IsCompleted MethodInfo: qword_EE1A168
//
// GuideRecord : RequirementRecordBase<GuideId>
//   +0x10 List<RequirementUnit>* _units
//   +0x18 GuideId FeatureId
// RequirementUnit:
//   +0x14 RequirementType Type
//   +0x20 long Current
//   +0x28 long Target
//
// Checklist: only listed requirement types get auto-actions.
// Unlisted guides remain manual (except completed ones still auto-claim).

enum class GuideAutoAction : int {
    None = 0,
    OvenStartAuto = 1, // 开启烤箱自动抽装备 (StartAuto)
    CookieGachaTenPull = 2,
    PetGachaTenPull = 3,
    UseRandomRewardBox = 4, // 背包使用宝箱 x1
};

// Patchdata.Protobuf.RequirementType
static constexpr int kReqEquipmentGacha = 14;
static constexpr int kReqEquipmentGachaFromNow = 15;
static constexpr int kReqAnyCookieGachaCount = 21;
static constexpr int kReqAnyCookieGachaCountFromNow = 22;
static constexpr int kReqAnyPetGachaCount = 23;
static constexpr int kReqAnyPetGachaCountFromNow = 24;
static constexpr int kReqMonsterKillFromNow = 19;
static constexpr int kReqRepeatGuideStageMonsterKillFromNow = 130;
static constexpr int kReqRandomRewardItemUsedFromNow = 129; // 背包使用宝箱

// Cookie/Pet gacha purchase button index: 0=单抽, 1=十连, 2=30连.
static constexpr int kGachaTenPullButtonIndex = 1;

// Only the repeating "kill 2000 monsters" guide is excluded from auto-claim.
// Progressive monster-kill tasks (e.g. kill 30) must still auto-claim.
// Match by target 2000 + monster-kill requirement family, not all MonsterKill.
static bool IsAutoClaimExcluded(int requirementType, int64_t target) {
    const bool isMonsterKill =
        requirementType == kReqMonsterKillFromNow ||
        requirementType == kReqRepeatGuideStageMonsterKillFromNow;
    return isMonsterKill && target >= 2000;
}

// ---------------------------------------------------------------------------
// Requirement residual-record fix
// ---------------------------------------------------------------------------
// Bug: RequirementUpdaterBase.Register replaces the record in
// RequirementRecordSet (Remove+Add) but never calls RemoveRecordByType on the
// old object. RecordsByType therefore keeps ghosts. When values is null for
// types 129/130, CalculateCurrentValue throws NotImplemented, TryCreateRecord
// fails, and the inflated ghost remains the only client view.
//
// Fix 1: before Register, if an old record exists, call RemoveRecordByType.
// Fix 2: CalculateCurrentValue(129/130) returns 0 instead of throwing so a
// replacement record can be created from server unitCounts / zero baseline.
//
// RVAs (UnityFramework 1.0.101 UUID-gated build):
//   Register              0x3E16814
//   TryGetRecord          0x3E2491C
//   RemoveRecordByType    0x3E163D8
//   CalculateCurrentValue 0x3E1ADF4
static constexpr uintptr_t kRequirementRegisterRVA = 0x03E16814;
static constexpr uintptr_t kRequirementTryGetRecordRVA = 0x03E2491C;
static constexpr uintptr_t kRequirementRemoveRecordByTypeRVA = 0x03E163D8;
static constexpr uintptr_t kRequirementCalculateCurrentValueRVA = 0x03E1ADF4;

static bool HookedRequirementRegister(void *self, unsigned int id,
                                      void *values) {
    const uintptr_t base = atomic_load(&gUnityFrameworkBase);
    if (self != nullptr && base != 0) {
        using TryGetRecordFn =
            bool (*)(void *updater, unsigned int reqId, void **outRecord);
        using RemoveRecordByTypeFn =
            void (*)(void *updater, void *record);

        void *oldRecord = nullptr;
        auto tryGet = reinterpret_cast<TryGetRecordFn>(
            base + kRequirementTryGetRecordRVA);
        if (tryGet(self, id, &oldRecord) && oldRecord != nullptr) {
            auto removeByType = reinterpret_cast<RemoveRecordByTypeFn>(
                base + kRequirementRemoveRecordByTypeRVA);
            removeByType(self, oldRecord);
            const int n =
                atomic_fetch_add(&gRequirementStaleCleanupCount, 1) + 1;
            // Rate-limited by JbrfzLog identical-line throttle.
            JbrfzLog(@"[JBRFZBypass] Requirement Register: removed stale "
                     @"RecordsByType entry id=%u (cleanup #%d)",
                     id, n);
        }
    }

    if (gOriginalRequirementRegister == nullptr) {
        return false;
    }
    return gOriginalRequirementRegister(self, id, values);
}

static int64_t HookedCalculateCurrentValue(void *self, int64_t type,
                                           void *key) {
    // 129 RandomRewardItemUsedFromNow / 130 RepeatGuideStageMonsterKillFromNow
    // throw NotImplemented in stock client. Returning 0 lets TryCreateRecord
    // succeed when server omits unitCounts, so Register can replace the record.
    if (type == kReqRandomRewardItemUsedFromNow ||
        type == kReqRepeatGuideStageMonsterKillFromNow) {
        return 0;
    }
    if (gOriginalCalculateCurrentValue == nullptr) {
        return 0;
    }
    return gOriginalCalculateCurrentValue(self, type, key);
}

struct GuideChecklistEntry {
    int requirementType;
    GuideAutoAction action;
};

// Expand this table as more tasks are reverse-engineered.
// Action path: incomplete guide click -> OpenShortcut to feature page,
// then run the feature-specific action.
static constexpr GuideChecklistEntry kGuideChecklist[] = {
    {kReqEquipmentGacha, GuideAutoAction::OvenStartAuto},            // 烤箱装备: 开自动抽
    {kReqEquipmentGachaFromNow, GuideAutoAction::OvenStartAuto},     // 烤箱装备: 开自动抽
    {kReqAnyCookieGachaCount, GuideAutoAction::CookieGachaTenPull},  // 饼干抽卡: 十连
    {kReqAnyCookieGachaCountFromNow, GuideAutoAction::CookieGachaTenPull},
    {kReqAnyPetGachaCount, GuideAutoAction::PetGachaTenPull},        // 宠物抽卡: 十连
    {kReqAnyPetGachaCountFromNow, GuideAutoAction::PetGachaTenPull},
    {kReqRandomRewardItemUsedFromNow, GuideAutoAction::UseRandomRewardBox}, // 背包开宝箱 x1
};

static atomic_int gLastAutoClaimGuideId = 0;
static atomic_llong gLastAutoClaimMs = 0;
static atomic_int gLastAutoActionGuideId = 0;
static atomic_llong gLastAutoActionMs = 0;
static atomic_int gLastLoggedGuideId = 0;

static GuideAutoAction FindChecklistAction(int requirementType) {
    for (const auto &entry : kGuideChecklist) {
        if (entry.requirementType == requirementType) {
            return entry.action;
        }
    }
    return GuideAutoAction::None;
}

static int BumpAutoActionGeneration() {
    return atomic_fetch_add(&gAutoActionGeneration, 1) + 1;
}

static bool IsAutoActionGenerationCurrent(int generation) {
    return atomic_load(&gAutoActionGeneration) == generation;
}

static void CancelPendingAutoActions(const char *reason) {
    if (atomic_load(&gPendingUseRandomRewardBox)) {
        JbrfzLog(@"[JBRFZBypass] Cancel pending open-box (%s)",
                 reason ? reason : "unknown");
    }
    atomic_store(&gPendingUseRandomRewardBox, false);
    BumpAutoActionGeneration();
}

static void JbrfzCancelAutoActionsImpl(const char *reason) {
    CancelPendingAutoActions(reason ? reason : "panel");
}

static int gJbrfzCancelBridgeInit = []() {
    gJbrfzCancelAutoActionsFn = &JbrfzCancelAutoActionsImpl;
    return 0;
}();


static bool ReadGuideUnit(void *record, int *outType, int64_t *outCurrent,
                          int64_t *outTarget) {
    if (record == nullptr || outType == nullptr || outCurrent == nullptr ||
        outTarget == nullptr) {
        return false;
    }

    // List<RequirementUnit>* at +0x10
    void *list = *reinterpret_cast<void **>(
        reinterpret_cast<uint8_t *>(record) + 0x10);
    if (list == nullptr) {
        return false;
    }

    const auto *listBytes = reinterpret_cast<const uint8_t *>(list);
    void *items = *reinterpret_cast<void *const *>(listBytes + 0x10);
    const int size = *reinterpret_cast<const int *>(listBytes + 0x18);
    if (items == nullptr || size <= 0) {
        return false;
    }

    // Il2Cpp array: first element pointer at +0x20
    void *unit = *reinterpret_cast<void *const *>(
        reinterpret_cast<const uint8_t *>(items) + 0x20);
    if (unit == nullptr) {
        return false;
    }

    const auto *unitBytes = reinterpret_cast<const uint8_t *>(unit);
    *outType = *reinterpret_cast<const int *>(unitBytes + 0x14);
    *outCurrent = *reinterpret_cast<const int64_t *>(unitBytes + 0x20);
    *outTarget = *reinterpret_cast<const int64_t *>(unitBytes + 0x28);
    return true;
}

// Manual guide clicks always pass through (including kill-2000 claim).
// RPC failures are handled by AfterResponse: clear loading, no error popup.
static void HookedHandleOnGuideUIClick(void *self, void *methodInfo) {
    if (self != nullptr) {
        atomic_store(&gGuidePresenter, reinterpret_cast<uintptr_t>(self));
    }
    if (gOriginalHandleOnGuideUIClick != nullptr) {
        gOriginalHandleOnGuideUIClick(self, methodInfo);
    }
}

static void HookedOvenAutoDrawCtor(void *self, void *equipmentApi,
                                   void *transactor, void *equipmentTable,
                                   void *combatPowerCalculator,
                                   void *gameSettingTable, void *ovenRecord,
                                   void *equipmentPresetRecordSet,
                                   void *totalItemRecord,
                                   void *ovenLevelUpTable,
                                   void *transactionNotifier,
                                   void *methodInfo) {
    if (gOriginalOvenAutoDrawCtor != nullptr) {
        gOriginalOvenAutoDrawCtor(self, equipmentApi, transactor, equipmentTable,
                                  combatPowerCalculator, gameSettingTable,
                                  ovenRecord, equipmentPresetRecordSet,
                                  totalItemRecord, ovenLevelUpTable,
                                  transactionNotifier, methodInfo);
    }
    if (self != nullptr) {
        atomic_store(&gOvenAutoDrawService,
                     reinterpret_cast<uintptr_t>(self));
        JbrfzLog(@"[JBRFZBypass] Captured OvenAutoDrawService %p", self);
    }
}

static void HookedCookieGachaOnPageOpen(void *self, void *methodInfo) {
    if (gOriginalCookieGachaOnPageOpen != nullptr) {
        gOriginalCookieGachaOnPageOpen(self, methodInfo);
    }
    if (self != nullptr) {
        atomic_store(&gCookieGachaPresenter,
                     reinterpret_cast<uintptr_t>(self));
        JbrfzLog(@"[JBRFZBypass] Captured CookieGachaPresenter %p", self);
    }
}

static void HookedPetGachaOnPageOpen(void *self, void *methodInfo) {
    if (gOriginalPetGachaOnPageOpen != nullptr) {
        gOriginalPetGachaOnPageOpen(self, methodInfo);
    }
    if (self != nullptr) {
        atomic_store(&gPetGachaPresenter,
                     reinterpret_cast<uintptr_t>(self));
        JbrfzLog(@"[JBRFZBypass] Captured PetGachaPresenter %p", self);
    }
}

// InventoryPresenter layout:
//   +0x18 TotalItemRecord*
//   +0x28 RandomRewardItemTable*
// RandomRewardItemTable (SimpleKeyValueTable): Rows List* @ +0x18
// RandomRewardItemInfo.Id @ +0x10 (ItemId / int32)
// TotalItemRecord.get_Item(ItemId) @ 0x03DE7BF8
// OnItemSelected(ItemId) @ 0x0411C714
// InventoryItemInfoPresenter.OnUseClicked(long amount) @ 0x04115B04
using ItemSelectMethod = void (*)(void *self, int itemId, void *methodInfo);
using ItemUseMethod = void (*)(void *self, int64_t amount, void *methodInfo);
using TotalItemGetMethod =
    int64_t (*)(void *self, int itemId, void *methodInfo);

static bool FindOwnedRandomRewardItemId(void *inventoryPresenter, uintptr_t base,
                                        int *outItemId) {
    if (inventoryPresenter == nullptr || outItemId == nullptr || base == 0) {
        return false;
    }
    const auto *p = reinterpret_cast<const uint8_t *>(inventoryPresenter);
    void *totalItemRecord = *reinterpret_cast<void *const *>(p + 0x18);
    void *randomTable = *reinterpret_cast<void *const *>(p + 0x28);
    if (totalItemRecord == nullptr || randomTable == nullptr) {
        return false;
    }

    void *rowsList = *reinterpret_cast<void **>(
        reinterpret_cast<uint8_t *>(randomTable) + 0x18);
    if (rowsList == nullptr) {
        return false;
    }
    const auto *listBytes = reinterpret_cast<const uint8_t *>(rowsList);
    void *items = *reinterpret_cast<void *const *>(listBytes + 0x10);
    const int size = *reinterpret_cast<const int *>(listBytes + 0x18);
    if (items == nullptr || size <= 0) {
        return false;
    }

    auto getAmount =
        reinterpret_cast<TotalItemGetMethod>(base + 0x03DE7BF8);
    const auto *arr = reinterpret_cast<const uint8_t *>(items);
    for (int i = 0; i < size; ++i) {
        void *info = *reinterpret_cast<void *const *>(arr + 0x20 +
                                                      static_cast<size_t>(i) * 8);
        if (info == nullptr) {
            continue;
        }
        const int itemId = *reinterpret_cast<const int *>(
            reinterpret_cast<const uint8_t *>(info) + 0x10);
        if (itemId == 0) {
            continue;
        }
        const int64_t amount = getAmount(totalItemRecord, itemId, nullptr);
        if (amount > 0) {
            *outItemId = itemId;
            return true;
        }
    }
    return false;
}

static void TrySelectRandomRewardItem(void *inventoryPresenter, uintptr_t base) {
    int itemId = 0;
    if (!FindOwnedRandomRewardItemId(inventoryPresenter, base, &itemId)) {
        JbrfzLog(@"[JBRFZBypass] Open-box: no owned RandomReward item found");
        atomic_store(&gPendingUseRandomRewardBox, false);
        return;
    }
    JbrfzLog(@"[JBRFZBypass] Open-box: select itemId=%d", itemId);
    auto onSelect =
        reinterpret_cast<ItemSelectMethod>(base + 0x0411C714);
    onSelect(inventoryPresenter, itemId, nullptr);
}

static void TryUseSelectedItem(void *itemInfoPresenter, uintptr_t base) {
    if (itemInfoPresenter == nullptr || base == 0) {
        return;
    }
    JbrfzLog(@"[JBRFZBypass] Open-box: Use x1");
    auto onUse = reinterpret_cast<ItemUseMethod>(base + 0x04115B04);
    onUse(itemInfoPresenter, /*amount=*/1, nullptr);
    atomic_store(&gPendingUseRandomRewardBox, false);
}

static void HookedInventoryOnPopupOpen(void *self, void *methodInfo) {
    if (gOriginalInventoryOnPopupOpen != nullptr) {
        gOriginalInventoryOnPopupOpen(self, methodInfo);
    }
    if (self != nullptr) {
        atomic_store(&gInventoryPresenter, reinterpret_cast<uintptr_t>(self));
        JbrfzLog(@"[JBRFZBypass] Captured InventoryPresenter %p", self);
    }
    if (!atomic_load(&gPendingUseRandomRewardBox)) {
        return;
    }
    const uintptr_t base = atomic_load(&gUnityBaseForGuide);
    if (base == 0 || self == nullptr) {
        return;
    }
    const int generation = atomic_load(&gAutoActionGeneration);
    // Inventory list may still be binding; slight delay then select a box.
    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.35 * NSEC_PER_SEC)),
        dispatch_get_main_queue(), ^{
          if (!atomic_load(&gPendingUseRandomRewardBox) ||
              !IsAutoActionGenerationCurrent(generation) ||
              !JbrfzAutoFeaturesEnabled()) {
              return;
          }
          void *inv = reinterpret_cast<void *>(
              atomic_load(&gInventoryPresenter));
          if (inv == nullptr || inv != self) {
              JbrfzLog(@"[JBRFZBypass] Open-box: inventory stale before select");
              return;
          }
          TrySelectRandomRewardItem(inv, base);
        });
}

static void HookedInventoryItemInfoOnPopupOpen(void *self, void *methodInfo) {
    if (gOriginalInventoryItemInfoOnPopupOpen != nullptr) {
        gOriginalInventoryItemInfoOnPopupOpen(self, methodInfo);
    }
    if (self != nullptr) {
        atomic_store(&gInventoryItemInfoPresenter,
                     reinterpret_cast<uintptr_t>(self));
        JbrfzLog(@"[JBRFZBypass] Captured InventoryItemInfoPresenter %p", self);
    }
    if (!atomic_load(&gPendingUseRandomRewardBox)) {
        return;
    }
    const uintptr_t base = atomic_load(&gUnityBaseForGuide);
    if (base == 0 || self == nullptr) {
        return;
    }
    const int generation = atomic_load(&gAutoActionGeneration);
    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.25 * NSEC_PER_SEC)),
        dispatch_get_main_queue(), ^{
          if (!atomic_load(&gPendingUseRandomRewardBox) ||
              !IsAutoActionGenerationCurrent(generation) ||
              !JbrfzAutoFeaturesEnabled()) {
              return;
          }
          void *info = reinterpret_cast<void *>(
              atomic_load(&gInventoryItemInfoPresenter));
          if (info == nullptr || info != self) {
              JbrfzLog(@"[JBRFZBypass] Open-box: item info stale before use");
              return;
          }
          TryUseSelectedItem(info, base);
        });
}

static void RunUseRandomRewardBoxAction(void *presenter, uintptr_t base,
                                        int guideId, int64_t current,
                                        int64_t target) {
    const int generation = BumpAutoActionGeneration();
    atomic_store(&gPendingUseRandomRewardBox, true);

    auto onClick =
        reinterpret_cast<GuideClickMethod>(base + 0x04215764);
    onClick(presenter, nullptr);

    const int64_t remaining = target > current ? (target - current) : 0;
    JbrfzLog(@"[JBRFZBypass] Open-box action guide %d (%lld/%lld, remain %lld)",
             guideId, static_cast<long long>(current),
             static_cast<long long>(target), static_cast<long long>(remaining));

    // Fallback if inventory was already open / OnPopupOpen already fired:
    // retry select after OpenShortcut settles.
    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.6 * NSEC_PER_SEC)),
        dispatch_get_main_queue(), ^{
          if (!atomic_load(&gPendingUseRandomRewardBox) ||
              !IsAutoActionGenerationCurrent(generation) ||
              !JbrfzAutoFeaturesEnabled()) {
              return;
          }
          void *inv = reinterpret_cast<void *>(
              atomic_load(&gInventoryPresenter));
          if (inv == nullptr) {
              JbrfzLog(@"[JBRFZBypass] Open-box fallback: inventory not captured");
              return;
          }
          TrySelectRandomRewardItem(inv, base);
        });
}


static void ClickGachaTenPull(void *presenter, uintptr_t base, bool isCookie) {
    if (presenter == nullptr || base == 0) {
        return;
    }
    // CookieGachaPresenter.HandleOnClickPurchase @ 0x04077AD8
    // PetGachaPresenter.HandleOnClickPurchase   @ 0x040ACA10
    const uintptr_t rva = isCookie ? 0x04077AD8ULL : 0x040ACA10ULL;
    auto purchase = reinterpret_cast<GachaPurchaseMethod>(base + rva);
    purchase(presenter, kGachaTenPullButtonIndex, nullptr);
    JbrfzLog(@"[JBRFZBypass] %s gacha TEN-pull (buttonIndex=%d)",
          isCookie ? "Cookie" : "Pet", kGachaTenPullButtonIndex);
}

static int ResolveOvenPresetIndex(void *service) {
    if (service == nullptr) {
        return 0;
    }
    // OvenAutoDrawService: _ovenRecord @ 0x88, _presetIndex @ 0xBC
    // OvenRecord.LastDrawnEquipmentPresetIndex @ 0x28
    const auto *svc = reinterpret_cast<const uint8_t *>(service);
    const int storedPreset = *reinterpret_cast<const int *>(svc + 0xBC);
    void *ovenRecord = *reinterpret_cast<void *const *>(svc + 0x88);
    if (ovenRecord != nullptr) {
        const int lastPreset = *reinterpret_cast<const int *>(
            reinterpret_cast<const uint8_t *>(ovenRecord) + 0x28);
        if (lastPreset >= 0) {
            return lastPreset;
        }
    }
    return storedPreset >= 0 ? storedPreset : 0;
}

// OvenAutoDrawService.CurrentMode @ +0x1C (AutoDrawMode: Stopped/Normal/Nonstop)
static bool IsOvenAutoActive(void *service) {
    if (service == nullptr) {
        return false;
    }
    const int mode = *reinterpret_cast<const int *>(
        reinterpret_cast<const uint8_t *>(service) + 0x1C);
    return mode != 0;
}

// Guide 14/15: open oven auto-draw (not a one-shot 1/10/30 pull).
// OvenAutoDrawService.StartAuto(presetIndex, nonstop) @ 0x041F70E0
using OvenStartAutoMethod =
    void (*)(void *self, int presetIndex, bool nonstop, void *methodInfo);

static void StartOvenAutoDraw(void *service, uintptr_t base) {
    if (service == nullptr || base == 0) {
        return;
    }
    if (IsOvenAutoActive(service)) {
        JbrfzLog(@"[JBRFZBypass] Oven auto already active; skip StartAuto");
        return;
    }

    const int preset = ResolveOvenPresetIndex(service);
    auto startAuto =
        reinterpret_cast<OvenStartAutoMethod>(base + 0x041F70E0);
    // nonstop=true -> AutoDrawMode.NonstopMode, keeps drawing until stopped.
    startAuto(service, preset, /*nonstop=*/true, nullptr);
    JbrfzLog(@"[JBRFZBypass] Oven StartAuto nonstop preset=%d", preset);
}

static void RunOvenStartAutoAction(void *presenter, uintptr_t base, int guideId,
                                   int64_t current, int64_t target) {
    void *service =
        reinterpret_cast<void *>(atomic_load(&gOvenAutoDrawService));
    if (IsOvenAutoActive(service)) {
        JbrfzLog(@"[JBRFZBypass] Oven auto already running for guide %d; no re-open",
              guideId);
        return;
    }

    const int generation = BumpAutoActionGeneration();

    // Navigate to oven via incomplete-guide click path.
    auto onClick =
        reinterpret_cast<GuideClickMethod>(base + 0x04215764);
    onClick(presenter, nullptr);

    const int64_t remaining = target > current ? (target - current) : 0;
    JbrfzLog(@"[JBRFZBypass] Oven StartAuto action guide %d (%lld/%lld, remain %lld)",
          guideId, static_cast<long long>(current),
          static_cast<long long>(target), static_cast<long long>(remaining));

    // Delay so OpenShortcut can present the oven page / bind services.
    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.2 * NSEC_PER_SEC)),
        dispatch_get_main_queue(), ^{
          if (!IsAutoActionGenerationCurrent(generation) ||
              !JbrfzAutoFeaturesEnabled()) {
              JbrfzLog(@"[JBRFZBypass] Oven StartAuto cancelled (stale gen)");
              return;
          }
          void *svc = reinterpret_cast<void *>(
              atomic_load(&gOvenAutoDrawService));
          if (svc == nullptr) {
              JbrfzLog(@"[JBRFZBypass] Oven StartAuto deferred: service not captured");
              return;
          }
          StartOvenAutoDraw(svc, base);
        });
}

static void RunCookieGachaTenPullAction(void *presenter, uintptr_t base,
                                        int guideId, int64_t current,
                                        int64_t target) {
    const int generation = BumpAutoActionGeneration();
    auto onClick =
        reinterpret_cast<GuideClickMethod>(base + 0x04215764);
    onClick(presenter, nullptr);

    const int64_t remaining = target > current ? (target - current) : 0;
    JbrfzLog(@"[JBRFZBypass] Cookie 十连 action guide %d (%lld/%lld, remain %lld)",
          guideId, static_cast<long long>(current),
          static_cast<long long>(target), static_cast<long long>(remaining));

    // Delay so OpenShortcut(ShowCookieGacha) can present the page and
    // CookieGachaPresenter.OnPageOpen can capture self.
    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.5 * NSEC_PER_SEC)),
        dispatch_get_main_queue(), ^{
          if (!IsAutoActionGenerationCurrent(generation) ||
              !JbrfzAutoFeaturesEnabled()) {
              JbrfzLog(@"[JBRFZBypass] Cookie 十连 cancelled (stale gen)");
              return;
          }
          void *gachaPresenter = reinterpret_cast<void *>(
              atomic_load(&gCookieGachaPresenter));
          if (gachaPresenter == nullptr) {
              JbrfzLog(@"[JBRFZBypass] Cookie 十连 deferred: presenter not captured");
              return;
          }
          ClickGachaTenPull(gachaPresenter, base, /*isCookie=*/true);
        });
}

static void RunPetGachaTenPullAction(void *presenter, uintptr_t base,
                                     int guideId, int64_t current,
                                     int64_t target) {
    const int generation = BumpAutoActionGeneration();
    auto onClick =
        reinterpret_cast<GuideClickMethod>(base + 0x04215764);
    onClick(presenter, nullptr);

    const int64_t remaining = target > current ? (target - current) : 0;
    JbrfzLog(@"[JBRFZBypass] Pet 十连 action guide %d (%lld/%lld, remain %lld)",
          guideId, static_cast<long long>(current),
          static_cast<long long>(target), static_cast<long long>(remaining));

    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.5 * NSEC_PER_SEC)),
        dispatch_get_main_queue(), ^{
          if (!IsAutoActionGenerationCurrent(generation) ||
              !JbrfzAutoFeaturesEnabled()) {
              JbrfzLog(@"[JBRFZBypass] Pet 十连 cancelled (stale gen)");
              return;
          }
          void *gachaPresenter = reinterpret_cast<void *>(
              atomic_load(&gPetGachaPresenter));
          if (gachaPresenter == nullptr) {
              JbrfzLog(@"[JBRFZBypass] Pet 十连 deferred: presenter not captured");
              return;
          }
          ClickGachaTenPull(gachaPresenter, base, /*isCookie=*/false);
        });
}

static void TryHandleCurrentGuide(void *presenter) {
    if (presenter == nullptr) {
        return;
    }

    // Master switch OFF: no auto-claim / auto-action for newbie-safe mode.
    if (!JbrfzAutoFeaturesEnabled()) {
        if (atomic_load(&gPendingUseRandomRewardBox)) {
            CancelPendingAutoActions("auto-disabled");
        }
        return;
    }

    atomic_store(&gGuidePresenter, reinterpret_cast<uintptr_t>(presenter));

    // GuidePresenter layout (1.0.101):
    // 0x80 _isWaitingForResponse, 0x88 _currentGuideRecord
    auto *bytes = reinterpret_cast<uint8_t *>(presenter);
    if (bytes[0x80] != 0) {
        return;
    }

    void *record = *reinterpret_cast<void **>(bytes + 0x88);
    if (record == nullptr) {
        return;
    }

    const uintptr_t base = atomic_load(&gUnityBaseForGuide);
    if (base == 0) {
        return;
    }

    const int guideId = *reinterpret_cast<const int *>(
        reinterpret_cast<const uint8_t *>(record) + 0x18);

    int reqType = -1;
    int64_t current = 0;
    int64_t target = 0;
    const bool hasUnit = ReadGuideUnit(record, &reqType, &current, &target);

    void *methodInfo = *reinterpret_cast<void **>(base + 0x0EE1A168);
    if (methodInfo == nullptr) {
        JbrfzLog(@"[JBRFZBypass] Guide skip: IsCompleted MethodInfo null");
        return;
    }

    auto isCompleted =
        reinterpret_cast<RecordBoolMethod>(base + 0x0904C7FC);
    const bool completed = isCompleted(record, methodInfo);

    if (atomic_load(&gLastLoggedGuideId) != guideId) {
        atomic_store(&gLastLoggedGuideId, guideId);
        const char *typeName = "";
        if (reqType == kReqRandomRewardItemUsedFromNow) {
            typeName = " RandomRewardBox";
        } else if (reqType == kReqRepeatGuideStageMonsterKillFromNow ||
                   reqType == kReqMonsterKillFromNow) {
            typeName = " MonsterKill";
        } else if (reqType == kReqEquipmentGacha ||
                   reqType == kReqEquipmentGachaFromNow) {
            typeName = " OvenGacha";
        } else if (reqType == kReqAnyCookieGachaCount ||
                   reqType == kReqAnyCookieGachaCountFromNow) {
            typeName = " CookieGacha";
        } else if (reqType == kReqAnyPetGachaCount ||
                   reqType == kReqAnyPetGachaCountFromNow) {
            typeName = " PetGacha";
        }
        JbrfzLog(@"[JBRFZBypass] Current guide %d type=%d%s %lld/%lld completed=%d",
              guideId, reqType, typeName,
              static_cast<long long>(current),
              static_cast<long long>(target), completed ? 1 : 0);
    }

    const long long nowMs =
        static_cast<long long>([[NSDate date] timeIntervalSince1970] * 1000.0);

    // If the active guide is no longer an incomplete open-box task, drop any
    // delayed inventory chain so manual UI ops cannot race stale blocks.
    const bool isOpenBoxGuide =
        hasUnit && reqType == kReqRandomRewardItemUsedFromNow && !completed;
    if (!isOpenBoxGuide && atomic_load(&gPendingUseRandomRewardBox)) {
        CancelPendingAutoActions("guide-changed");
    }

    if (completed) {
        // Exclude buggy/repeat monster-kill guides from auto-claim.
        if (hasUnit && IsAutoClaimExcluded(reqType, target)) {
            // Progress updates every kill; log once per guide only.
            if (atomic_load(&gLastSkipClaimGuideId) != guideId) {
                atomic_store(&gLastSkipClaimGuideId, guideId);
                JbrfzLog(@"[JBRFZBypass] Skip auto-claim guide %d type=%d "
                         "(%lld/%lld) excluded kill-2000-repeat",
                         guideId, reqType, static_cast<long long>(current),
                         static_cast<long long>(target));
            }
            return;
        }

        if (atomic_load(&gLastAutoClaimGuideId) == guideId &&
            nowMs - atomic_load(&gLastAutoClaimMs) < 2500) {
            return;
        }
        atomic_store(&gLastAutoClaimGuideId, guideId);
        atomic_store(&gLastAutoClaimMs, nowMs);

        // Claiming switches UI; invalidate any delayed auto-action blocks.
        CancelPendingAutoActions("auto-claim");
        JbrfzLog(@"[JBRFZBypass] Auto-claiming completed guide %d type=%d "
              "(%lld/%lld)",
              guideId, reqType, static_cast<long long>(current),
              static_cast<long long>(target));
        auto onClick =
            reinterpret_cast<GuideClickMethod>(base + 0x04215764);
        onClick(presenter, nullptr);
        return;
    }

    if (!hasUnit) {
        return;
    }

    const GuideAutoAction action = FindChecklistAction(reqType);
    if (action == GuideAutoAction::None) {
        // Not in checklist -> leave for manual play.
        return;
    }

    // Debounce action kicks for the same incomplete guide.
    // For multi-step targets (e.g. 15 with batch 10), a later progress
    // update after debounce can fire another single batch.
    if (atomic_load(&gLastAutoActionGuideId) == guideId &&
        nowMs - atomic_load(&gLastAutoActionMs) < 4000) {
        return;
    }
    atomic_store(&gLastAutoActionGuideId, guideId);
    atomic_store(&gLastAutoActionMs, nowMs);

    switch (action) {
    case GuideAutoAction::OvenStartAuto:
        RunOvenStartAutoAction(presenter, base, guideId, current, target);
        break;
    case GuideAutoAction::CookieGachaTenPull:
        RunCookieGachaTenPullAction(presenter, base, guideId, current, target);
        break;
    case GuideAutoAction::PetGachaTenPull:
        RunPetGachaTenPullAction(presenter, base, guideId, current, target);
        break;
    case GuideAutoAction::UseRandomRewardBox:
        RunUseRandomRewardBoxAction(presenter, base, guideId, current, target);
        break;
    case GuideAutoAction::None:
        break;
    }
}

static void HookedHandleOnProgressUpdated(void *self,
                                          void *modifiedRecords,
                                          void *completedRecords,
                                          void *methodInfo) {
    if (gOriginalHandleOnProgressUpdated != nullptr) {
        gOriginalHandleOnProgressUpdated(self, modifiedRecords,
                                         completedRecords, methodInfo);
    }
    TryHandleCurrentGuide(self);
}

static void HookedUpdateUI(void *self, void *methodInfo) {
    if (gOriginalUpdateUI != nullptr) {
        gOriginalUpdateUI(self, methodInfo);
    }
    TryHandleCurrentGuide(self);
}


static void InstallProtectionThreadHooks(const struct mach_header *header,
                                         bool isMainExecutable) {
    // AppSealing 1.14.0 creates ten protection-only pthreads from each
    // statically linked SDK copy. Several of them deliberately terminate,
    // fault or loop forever after a delayed environment check.
    static constexpr uintptr_t unityThreadRVAs[] = {
        0x000EEC38, 0x000EED04, 0x000EEE60, 0x000EEE84, 0x000EF060,
        0x000EF158, 0x000EF2E8, 0x000EF448, 0x000EF544, 0x000EF568,
    };
    static constexpr uintptr_t mainThreadRVAs[] = {
        0x000837FC, 0x000838C8, 0x00083A24, 0x00083A48, 0x00083C24,
        0x00083D1C, 0x00083EAC, 0x0008400C, 0x00084108, 0x0008412C,
    };

    const uintptr_t *rvas =
        isMainExecutable ? mainThreadRVAs : unityThreadRVAs;
    const size_t originalIndex = isMainExecutable ? 0 : 10;
    const uintptr_t base = reinterpret_cast<uintptr_t>(header);

    for (size_t index = 0; index < 10; ++index) {
        MSHookFunction(
            reinterpret_cast<void *>(base + rvas[index]),
            reinterpret_cast<void *>(&ReturnNullThread),
            &gOriginalProtectionThreads[originalIndex + index]);
    }

    JbrfzLog(@"[JBRFZBypass] AppSealing protection threads neutralized "
          "(image=%@)",
          isMainExecutable ? @"main" : @"unity");
}

static bool HasExpectedUUID(const struct mach_header *header,
                            const uint8_t expectedUUID[16]) {
    const auto *header64 =
        reinterpret_cast<const struct mach_header_64 *>(header);
    const uint8_t *cursor =
        reinterpret_cast<const uint8_t *>(header64 + 1);

    for (uint32_t index = 0; index < header64->ncmds; ++index) {
        const auto *command =
            reinterpret_cast<const struct load_command *>(cursor);
        if (command->cmdsize < sizeof(struct load_command)) {
            return false;
        }

        if (command->cmd == LC_UUID &&
            command->cmdsize >= sizeof(struct uuid_command)) {
            const auto *uuid =
                reinterpret_cast<const struct uuid_command *>(command);
            return memcmp(uuid->uuid, expectedUUID, 16) == 0;
        }

        cursor += command->cmdsize;
    }

    return false;
}

static bool IsSupportedMainExecutable(const struct mach_header *header) {
    // CookieRun: Crumble 1.0.101 (19), main executable UUID
    // E8B5F894-2448-36FC-8190-3D073663316B.
    static constexpr uint8_t expectedUUID[16] = {
        0xE8, 0xB5, 0xF8, 0x94, 0x24, 0x48, 0x36, 0xFC,
        0x81, 0x90, 0x3D, 0x07, 0x36, 0x63, 0x31, 0x6B,
    };
    return HasExpectedUUID(header, expectedUUID);
}

static void ForceAppSealingNormalState(void) {
    static constexpr uint64_t normalState = 0xB3;
    static constexpr uintptr_t mainStateRVA = 0x003AA300;
    static constexpr uintptr_t unityStateRVA = 0x0F6580D8;

    const uintptr_t mainBase = atomic_load(&gMainExecutableBase);
    const uintptr_t unityBase = atomic_load(&gUnityFrameworkBase);

    if (mainBase != 0) {
        __atomic_store_n(
            reinterpret_cast<uint64_t *>(mainBase + mainStateRVA),
            normalState, __ATOMIC_RELEASE);
    }
    if (unityBase != 0) {
        __atomic_store_n(
            reinterpret_cast<uint64_t *>(unityBase + unityStateRVA),
            normalState, __ATOMIC_RELEASE);
    }

    JbrfzLog(@"[JBRFZBypass] AppSealing environment state forced normal "
          "(main=%@, unity=%@)",
          mainBase != 0 ? @"yes" : @"no",
          unityBase != 0 ? @"yes" : @"no");
}

static void ScheduleNormalState(void) {
    if (atomic_load(&gMainExecutableBase) == 0 ||
        atomic_load(&gUnityFrameworkBase) == 0) {
        return;
    }

    bool expected = false;
    if (!atomic_compare_exchange_strong(&gNormalStateScheduled,
                                        &expected, true)) {
        return;
    }

    const dispatch_queue_t queue =
        dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0);
    static constexpr int64_t delays[] = {1, 3, 8};
    for (int64_t delaySeconds : delays) {
        dispatch_after(
            dispatch_time(DISPATCH_TIME_NOW,
                          delaySeconds * NSEC_PER_SEC),
            queue, ^{
                ForceAppSealingNormalState();
            });
    }
}

static bool IsSupportedUnityFramework(const struct mach_header *header) {
    // CookieRun: Crumble 1.0.101 (19), UnityFramework UUID
    // 84EF8300-3180-31B4-ABB8-B176E685CC12.
    static constexpr uint8_t expectedUUID[16] = {
        0x84, 0xEF, 0x83, 0x00, 0x31, 0x80, 0x31, 0xB4,
        0xAB, 0xB8, 0xB1, 0x76, 0xE6, 0x85, 0xCC, 0x12,
    };

    return HasExpectedUUID(header, expectedUUID);
}

static void InstallManagedFallbackHooks(const struct mach_header *header) {
    if (!IsSupportedUnityFramework(header)) {
        JbrfzLog(@"[JBRFZBypass] Unknown UnityFramework build; "
              "skipping version-specific managed hooks");
        return;
    }

    const uintptr_t base = reinterpret_cast<uintptr_t>(header);

    // AppSealingSDK.Check_iOS_Security methods for the UUID above.
    // These narrowly prevent the SDK from starting its delayed monitor or
    // executing its managed exit callback. They are deliberately UUID-gated.
    static constexpr uintptr_t startRoutineRVA = 0x03A002B0;
    static constexpr uintptr_t appSealingRoutineRVA = 0x03A00474;
    static constexpr uintptr_t exitFunctionRVA = 0x03A003A8;
    static constexpr uintptr_t runtimeLoadRVA = 0x03A00C9C;

    MSHookFunction(reinterpret_cast<void *>(base + startRoutineRVA),
                   reinterpret_cast<void *>(&ReturnWithoutAction),
                   &gOriginalStartRoutine);
    MSHookFunction(reinterpret_cast<void *>(base + appSealingRoutineRVA),
                   reinterpret_cast<void *>(&ReturnWithoutAction),
                   &gOriginalAppSealingRoutine);
    MSHookFunction(reinterpret_cast<void *>(base + exitFunctionRVA),
                   reinterpret_cast<void *>(&ReturnWithoutAction),
                   &gOriginalExitFunction);
    MSHookFunction(reinterpret_cast<void *>(base + runtimeLoadRVA),
                   reinterpret_cast<void *>(&ReturnWithoutAction),
                   &gOriginalRuntimeLoad);

    // Crumble.AdRemoveGameBoostCalculator.IsAdRemoveActive
    // CookieRun: Crumble 1.0.101 (19) / UnityFramework UUID above.
    // All client "免广告卡" checks funnel through this method:
    // AdService.get_IsRemovedAd, ShowRewardedAsync, red-dot updaters,
    // stage random reward claim UI, and package shop cells.
    static constexpr uintptr_t isAdRemoveActiveRVA = 0x03D85D74;
    MSHookFunction(
        reinterpret_cast<void *>(base + isAdRemoveActiveRVA),
        reinterpret_cast<void *>(&ReturnAdRemoveActive),
        reinterpret_cast<void **>(&gOriginalIsAdRemoveActive));

    JbrfzLog(@"[JBRFZBypass] Ad-remove boost forced active "
          "(IsAdRemoveActive @ 0x%lx)",
          static_cast<unsigned long>(isAdRemoveActiveRVA));

    // Needed early so RPC-error loading dismiss can resolve Unset RVA.
    atomic_store(&gUnityBaseForGuide, base);

    // LoadingFlag.Set: remember key so RPC-error path can Unset without popup.
    static constexpr uintptr_t loadingFlagSetRVA = 0x03BB0C7C;
    MSHookFunction(
        reinterpret_cast<void *>(base + loadingFlagSetRVA),
        reinterpret_cast<void *>(&HookedLoadingFlagSet),
        reinterpret_cast<void **>(&gOriginalLoadingFlagSet));

    // MainSceneExchangeEventListener.AfterResponse(RpcException)
    // Original: Unset loading + open "处理请求过程中出现错误" popup.
    // We Unset loading / clear guide wait, but never open popup / AfterError.
    static constexpr uintptr_t afterResponseErrorRVA = 0x03A465D0;
    MSHookFunction(
        reinterpret_cast<void *>(base + afterResponseErrorRVA),
        reinterpret_cast<void *>(&HookedAfterResponseRpcException),
        &gOriginalAfterResponseError);

    // MainSceneExchangeEventListener.AfterError
    // Prevent RestartApp / return-to-login, but still clear loading.
    static constexpr uintptr_t afterErrorRVA = 0x03A46A54;
    MSHookFunction(
        reinterpret_cast<void *>(base + afterErrorRVA),
        reinterpret_cast<void *>(&HookedAfterError),
        &gOriginalAfterError);

    // Track GuidePresenter on manual click; claim itself is never blocked.
    static constexpr uintptr_t handleOnGuideUIClickRVA = 0x04215764;
    MSHookFunction(
        reinterpret_cast<void *>(base + handleOnGuideUIClickRVA),
        reinterpret_cast<void *>(&HookedHandleOnGuideUIClick),
        reinterpret_cast<void **>(&gOriginalHandleOnGuideUIClick));

    JbrfzLog(@"[JBRFZBypass] Error popup suppressed + loading cleared "
          "(AfterResponse(RpcException) @ 0x%lx, AfterError @ 0x%lx, "
          "LoadingSet @ 0x%lx, GuideClick @ 0x%lx)",
          static_cast<unsigned long>(afterResponseErrorRVA),
          static_cast<unsigned long>(afterErrorRVA),
          static_cast<unsigned long>(loadingFlagSetRVA),
          static_cast<unsigned long>(handleOnGuideUIClickRVA));

    // ---- Capture hooks for invite / stage / account reverse ----
    // 1.0.101 keeps ExchangeLogger in the binary but no longer dispatches
    // production traffic to it. MainSceneExchangeEventListener is the active
    // listener for in-game RPCs, including guild join/leave.
    static constexpr uintptr_t exchangeBeforeRVA = 0x03A46430;
    MSHookFunction(
        reinterpret_cast<void *>(base + exchangeBeforeRVA),
        reinterpret_cast<void *>(&HookedExchangeBeforeRequest),
        reinterpret_cast<void **>(&gOriginalExchangeBeforeRequest));

    static constexpr uintptr_t exchangeAfterOkRVA = 0x03A46500;
    MSHookFunction(
        reinterpret_cast<void *>(base + exchangeAfterOkRVA),
        reinterpret_cast<void *>(&HookedExchangeAfterOk),
        reinterpret_cast<void **>(&gOriginalExchangeAfterOk));

    // Retain the legacy logger error hook as a fallback. The active main-scene
    // error overload is already hooked above by HookedAfterResponseRpcException,
    // which now records the failed RPC before suppressing the popup.
    static constexpr uintptr_t exchangeAfterErrRVA = 0x03DACB4C;
    MSHookFunction(
        reinterpret_cast<void *>(base + exchangeAfterErrRVA),
        reinterpret_cast<void *>(&HookedExchangeAfterErr),
        reinterpret_cast<void **>(&gOriginalExchangeAfterErr));

    static constexpr uintptr_t inviteLinkRVA = 0x03D6E50C;
    MSHookFunction(
        reinterpret_cast<void *>(base + inviteLinkRVA),
        reinterpret_cast<void *>(&HookedCreateFriendInvitationLink),
        reinterpret_cast<void **>(&gOriginalCreateFriendInvitationLink));

    static constexpr uintptr_t guestSetRVA = 0x04BC4CCC;
    MSHookFunction(
        reinterpret_cast<void *>(base + guestSetRVA),
        reinterpret_cast<void *>(&HookedGuestLoginKeyChainSet),
        reinterpret_cast<void **>(&gOriginalGuestLoginKeyChainSet));

    static constexpr uintptr_t processLoginRVA = 0x03D70FB4;
    MSHookFunction(
        reinterpret_cast<void *>(base + processLoginRVA),
        reinterpret_cast<void *>(&HookedProcessLoginResponse),
        reinterpret_cast<void **>(&gOriginalProcessLoginResponse));

    static constexpr uintptr_t grpcChannelCtorRVA = 0x03DADAE0;
    MSHookFunction(
        reinterpret_cast<void *>(base + grpcChannelCtorRVA),
        reinterpret_cast<void *>(&HookedGrpcChannelCtor),
        reinterpret_cast<void **>(&gOriginalGrpcChannelCtor));

    static constexpr uintptr_t endpointToStringRVA = 0x03DABCBC;
    MSHookFunction(
        reinterpret_cast<void *>(base + endpointToStringRVA),
        reinterpret_cast<void *>(&HookedEndpointToString),
        reinterpret_cast<void **>(&gOriginalEndpointToString));

    static constexpr uintptr_t createChannelRVA = 0x03DADCFC;
    MSHookFunction(
        reinterpret_cast<void *>(base + createChannelRVA),
        reinterpret_cast<void *>(&HookedCreateChannel),
        reinterpret_cast<void **>(&gOriginalCreateChannel));

    static constexpr uintptr_t createHeadersRVA = 0x03DAF1DC;
    MSHookFunction(
        reinterpret_cast<void *>(base + createHeadersRVA),
        reinterpret_cast<void *>(&HookedCreateHeaders),
        reinterpret_cast<void **>(&gOriginalCreateHeaders));

    JbrfzCaptureLog(@"[CAPTURE] hooks armed main-rpc@0x%lx/0x%lx "
                    @"legacy-error@0x%lx invite@0x%lx "
                    @"guest@0x%lx login@0x%lx endpoint@0x%lx url@0x%lx "
                    @"channel@0x%lx headers@0x%lx",
                    static_cast<unsigned long>(exchangeBeforeRVA),
                    static_cast<unsigned long>(exchangeAfterOkRVA),
                    static_cast<unsigned long>(exchangeAfterErrRVA),
                    static_cast<unsigned long>(inviteLinkRVA),
                    static_cast<unsigned long>(guestSetRVA),
                    static_cast<unsigned long>(processLoginRVA),
                    static_cast<unsigned long>(grpcChannelCtorRVA),
                    static_cast<unsigned long>(endpointToStringRVA),
                    static_cast<unsigned long>(createChannelRVA),
                    static_cast<unsigned long>(createHeadersRVA));

    // Direct StageApi / EventApi hooks for invite-bot samples.
    static constexpr uintptr_t startStageRVA = 0x03D227E8;
    MSHookFunction(
        reinterpret_cast<void *>(base + startStageRVA),
        reinterpret_cast<void *>(&HookedStartStageAsync),
        reinterpret_cast<void **>(&gOriginalStartStageAsync));

    static constexpr uintptr_t completeStageRVA = 0x03D22920;
    MSHookFunction(
        reinterpret_cast<void *>(base + completeStageRVA),
        reinterpret_cast<void *>(&HookedCompleteStageAsync),
        reinterpret_cast<void **>(&gOriginalCompleteStageAsync));

    static constexpr uintptr_t registerFriendRVA = 0x03CF7FBC;
    MSHookFunction(
        reinterpret_cast<void *>(base + registerFriendRVA),
        reinterpret_cast<void *>(&HookedRegisterFriendInviter),
        reinterpret_cast<void **>(&gOriginalRegisterFriendInviter));

    JbrfzCaptureLog(@"[CAPTURE] stage hooks Start@0x%lx Complete@0x%lx "
                    @"RegisterFriend@0x%lx",
                    static_cast<unsigned long>(startStageRVA),
                    static_cast<unsigned long>(completeStageRVA),
                    static_cast<unsigned long>(registerFriendRVA));
    JbrfzLog(@"[JBRFZBypass] Capture hooks armed (invite/stage/account)");

    // Capture OvenAutoDrawService for checklist OvenStartAuto actions.
    static constexpr uintptr_t ovenAutoDrawCtorRVA = 0x041F6950;
    MSHookFunction(
        reinterpret_cast<void *>(base + ovenAutoDrawCtorRVA),
        reinterpret_cast<void *>(&HookedOvenAutoDrawCtor),
        reinterpret_cast<void **>(&gOriginalOvenAutoDrawCtor));

    // Capture Cookie/Pet gacha presenters when their pages open
    // (guide card OpenShortcut lands here before we press 十连).
    static constexpr uintptr_t cookieGachaOnPageOpenRVA = 0x04074EE0;
    MSHookFunction(
        reinterpret_cast<void *>(base + cookieGachaOnPageOpenRVA),
        reinterpret_cast<void *>(&HookedCookieGachaOnPageOpen),
        reinterpret_cast<void **>(&gOriginalCookieGachaOnPageOpen));

    static constexpr uintptr_t petGachaOnPageOpenRVA = 0x040A9EE8;
    MSHookFunction(
        reinterpret_cast<void *>(base + petGachaOnPageOpenRVA),
        reinterpret_cast<void *>(&HookedPetGachaOnPageOpen),
        reinterpret_cast<void **>(&gOriginalPetGachaOnPageOpen));

    // Inventory open-box chain: ShowInventory -> select RandomReward -> Use x1
    static constexpr uintptr_t inventoryOnPopupOpenRVA = 0x0411BF9C;
    MSHookFunction(
        reinterpret_cast<void *>(base + inventoryOnPopupOpenRVA),
        reinterpret_cast<void *>(&HookedInventoryOnPopupOpen),
        reinterpret_cast<void **>(&gOriginalInventoryOnPopupOpen));

    static constexpr uintptr_t inventoryItemInfoOnPopupOpenRVA = 0x0411573C;
    MSHookFunction(
        reinterpret_cast<void *>(base + inventoryItemInfoOnPopupOpenRVA),
        reinterpret_cast<void *>(&HookedInventoryItemInfoOnPopupOpen),
        reinterpret_cast<void **>(&gOriginalInventoryItemInfoOnPopupOpen));

    // Requirement residual-record fix (always on; not gated by auto panel).
    MSHookFunction(
        reinterpret_cast<void *>(base + kRequirementRegisterRVA),
        reinterpret_cast<void *>(&HookedRequirementRegister),
        reinterpret_cast<void **>(&gOriginalRequirementRegister));
    MSHookFunction(
        reinterpret_cast<void *>(base + kRequirementCalculateCurrentValueRVA),
        reinterpret_cast<void *>(&HookedCalculateCurrentValue),
        reinterpret_cast<void **>(&gOriginalCalculateCurrentValue));
    JbrfzLog(@"[JBRFZBypass] Requirement residual fix armed "
             @"(Register @ 0x%lx, CalculateCurrentValue @ 0x%lx)",
             static_cast<unsigned long>(kRequirementRegisterRVA),
             static_cast<unsigned long>(kRequirementCalculateCurrentValueRVA));

    // GuidePresenter.HandleOnProgressUpdated / UpdateUI
    // - completed: auto-claim
    // - incomplete + checklist hit: run matched auto action
    static constexpr uintptr_t handleOnProgressUpdatedRVA =
        0x04215BEC;
    atomic_store(&gUnityBaseForGuide, base);
    atomic_store(&gLastAutoClaimGuideId, 0);
    atomic_store(&gLastAutoActionGuideId, 0);
    atomic_store(&gLastLoggedGuideId, 0);
    MSHookFunction(
        reinterpret_cast<void *>(base + handleOnProgressUpdatedRVA),
        reinterpret_cast<void *>(&HookedHandleOnProgressUpdated),
        reinterpret_cast<void **>(&gOriginalHandleOnProgressUpdated));

    static constexpr uintptr_t updateUIRVA = 0x042151D4;
    MSHookFunction(
        reinterpret_cast<void *>(base + updateUIRVA),
        reinterpret_cast<void *>(&HookedUpdateUI),
        reinterpret_cast<void **>(&gOriginalUpdateUI));

    JbrfzLog(@"[JBRFZBypass] Guide automation armed "
          "(progress @ 0x%lx, UpdateUI @ 0x%lx, OvenCtor @ 0x%lx, "
          "CookieOpen @ 0x%lx, PetOpen @ 0x%lx, InvOpen @ 0x%lx, "
          "ItemInfoOpen @ 0x%lx)",
          static_cast<unsigned long>(handleOnProgressUpdatedRVA),
          static_cast<unsigned long>(updateUIRVA),
          static_cast<unsigned long>(ovenAutoDrawCtorRVA),
          static_cast<unsigned long>(cookieGachaOnPageOpenRVA),
          static_cast<unsigned long>(petGachaOnPageOpenRVA),
          static_cast<unsigned long>(inventoryOnPopupOpenRVA),
          static_cast<unsigned long>(inventoryItemInfoOnPopupOpenRVA));
}

static bool IsImageWithSuffix(const struct mach_header *header,
                              const char *suffix,
                              const char **imagePath) {
    const uint32_t imageCount = _dyld_image_count();
    for (uint32_t index = 0; index < imageCount; ++index) {
        if (_dyld_get_image_header(index) != header) {
            continue;
        }

        const char *path = _dyld_get_image_name(index);
        if (path == nullptr) {
            return false;
        }

        const size_t pathLength = strlen(path);
        const size_t suffixLength = strlen(suffix);
        if (pathLength < suffixLength ||
            strcmp(path + pathLength - suffixLength, suffix) != 0) {
            return false;
        }

        *imagePath = path;
        return true;
    }

    return false;
}

static void InstallAppSealingHooks(const struct mach_header *header,
                                   intptr_t slide) {
    (void)slide;

    static constexpr char mainSuffix[] =
        "/CookieRunCrumble.app/CookieRunCrumble";
    const char *mainPath = nullptr;
    if (IsImageWithSuffix(header, mainSuffix, &mainPath)) {
        if (IsSupportedMainExecutable(header)) {
            InstallProtectionThreadHooks(header, true);
            atomic_store(&gMainExecutableBase,
                         reinterpret_cast<uintptr_t>(header));
            ScheduleNormalState();
        } else {
            JbrfzLog(@"[JBRFZBypass] Unknown main executable build; "
                  "skipping version-specific state patch");
        }
        return;
    }

    static constexpr char unitySuffix[] =
        "/UnityFramework.framework/UnityFramework";
    const char *imagePath = nullptr;
    if (!IsImageWithSuffix(header, unitySuffix, &imagePath)) {
        return;
    }

    if (!IsSupportedUnityFramework(header)) {
        JbrfzLog(@"[JBRFZBypass] Unknown UnityFramework build; "
              "leaving the process untouched");
        return;
    }

    void *image = dlopen(imagePath, RTLD_NOW | RTLD_NOLOAD);
    if (image == nullptr) {
        return;
    }

    void *abnormal =
        dlsym(image, "Unity_IsAbnormalEnvironmentDetected");
    void *swizzling = dlsym(image, "Unity_IsSwizzlingDetected");
    void *swizzlingIter = dlsym(image, "Unity_IsSwizzlingDetectedIter");

    if (abnormal == nullptr || swizzling == nullptr ||
        swizzlingIter == nullptr) {
        JbrfzLog(@"[JBRFZBypass] AppSealing exports were not found; "
              "leaving the process untouched");
        dlclose(image);
        return;
    }

    bool expected = false;
    if (!atomic_compare_exchange_strong(&gInstalled, &expected, true)) {
        dlclose(image);
        return;
    }

    MSHookFunction(abnormal,
                   reinterpret_cast<void *>(&ReturnNormalEnvironment),
                   reinterpret_cast<void **>(&gOriginalAbnormalEnvironment));
    MSHookFunction(swizzling,
                   reinterpret_cast<void *>(&ReturnNormalEnvironment),
                   reinterpret_cast<void **>(&gOriginalSwizzling));
    MSHookFunction(swizzlingIter,
                   reinterpret_cast<void *>(&ReturnNormalEnvironment),
                   reinterpret_cast<void **>(&gOriginalSwizzlingIter));
    InstallProtectionThreadHooks(header, false);
    InstallManagedFallbackHooks(header);
    atomic_store(&gUnityFrameworkBase,
                 reinterpret_cast<uintptr_t>(header));
    ScheduleNormalState();

    JbrfzLog(@"[JBRFZBypass] AppSealing native and managed checks neutralized");
    dlclose(image);
}

} // namespace

__attribute__((constructor))
static void JBRFZBypassInitialize(void) {
    @autoreleasepool {
        JbrfzLog(@"[JBRFZBypass] dylib loaded home=%@ version=0.3.25 auto=%d",
                 NSHomeDirectory() ?: @"(nil)",
                 JbrfzAutoFeaturesEnabled() ? 1 : 0);
        _dyld_register_func_for_add_image(&InstallAppSealingHooks);
        // Floating panel: master switch for auto features (default OFF).
        JbrfzStartPluginPanel();
    }
}
