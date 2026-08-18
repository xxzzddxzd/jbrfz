#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_IPA="${SOURCE_IPA:-$REPO_ROOT/11101/1.zip}"
MAIN_BINARY="${MAIN_BINARY:-$REPO_ROOT/11101/main/CookieRunCrumble}"
UNITY_BINARY="${UNITY_BINARY:-$REPO_ROOT/11101/UnityFramework}"
OUTPUT_IPA="${OUTPUT_IPA:-$SCRIPT_DIR/dist/CookieRunCrumble-1.1.101-JBRFZ.ipa}"
BUNDLE_ID_OVERRIDE="${BUNDLE_ID:-}"
IOS_SAFE_MODE="${IOS_SAFE_MODE:-0}"
MARKETPLACE_STUB="${MARKETPLACE_STUB:-}"
if [[ -z "$MARKETPLACE_STUB" ]]; then
    if [[ "$IOS_SAFE_MODE" == "1" ]]; then
        MARKETPLACE_STUB=0
    else
        MARKETPLACE_STUB=1
    fi
fi
PATCH_INSTALL_NAME="@executable_path/Frameworks/JBRFZPatch.dylib"
MARKETPLACE_SYSTEM_PATH="/System/Library/Frameworks/MarketplaceKit.framework/MarketplaceKit"
MARKETPLACE_INSTALL_NAME="@rpath/MarketplaceKit.framework/MarketplaceKit"

if [[ -n "${DOBBY_LIB:-}" ]]; then
    DOBBY_ARCHIVE="$DOBBY_LIB"
elif [[ -f /Users/xuzhengda/Documents/workspace/srzq/tweak/mac/vendor/dobby/libdobby.a ]]; then
    DOBBY_ARCHIVE=/Users/xuzhengda/Documents/workspace/srzq/tweak/mac/vendor/dobby/libdobby.a
elif [[ -f /Users/xuzhengda/Documents/workspace/smbb/macver/vendor/dobby/libdobby.a ]]; then
    DOBBY_ARCHIVE=/Users/xuzhengda/Documents/workspace/smbb/macver/vendor/dobby/libdobby.a
else
    echo "找不到 arm64 iOS libdobby.a；请通过 DOBBY_LIB 指定。" >&2
    exit 2
fi

for required in "$SOURCE_IPA" "$MAIN_BINARY" "$UNITY_BINARY" "$DOBBY_ARCHIVE"; do
    if [[ ! -f "$required" ]]; then
        echo "缺少文件：$required" >&2
        exit 2
    fi
done

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/jbrfz-ipa.XXXXXX")"
cleanup() {
    if [[ -n "$WORK_DIR" && -d "$WORK_DIR" && "$WORK_DIR" == *jbrfz-ipa.* ]]; then
        rm -rf "$WORK_DIR"
    fi
}
trap cleanup EXIT

mkdir -p "$(dirname "$OUTPUT_IPA")" "$SCRIPT_DIR/build"
ditto -x -k "$SOURCE_IPA" "$WORK_DIR"
APP_DIR="$WORK_DIR/Payload/CookieRunCrumble.app"
APP_MAIN="$APP_DIR/CookieRunCrumble"
APP_UNITY="$APP_DIR/Frameworks/UnityFramework.framework/UnityFramework"
PATCH_DYLIB="$SCRIPT_DIR/build/JBRFZPatch.dylib"
MARKETPLACE_FRAMEWORK="$APP_DIR/Frameworks/MarketplaceKit.framework"
MARKETPLACE_BINARY="$MARKETPLACE_FRAMEWORK/MarketplaceKit"

if [[ ! -d "$APP_DIR" || ! -f "$APP_UNITY" ]]; then
    echo "源归档不是预期的 CookieRunCrumble 1.1.101 IPA。" >&2
    exit 2
fi

VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP_DIR/Info.plist")"
BUILD="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP_DIR/Info.plist")"
if [[ "$VERSION" != "1.1.101" || "$BUILD" != "2026081413" ]]; then
    echo "版本不匹配：需要 1.1.101 (2026081413)，实际 $VERSION ($BUILD)。" >&2
    exit 2
fi

SOURCE_BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP_DIR/Info.plist")"
TARGET_BUNDLE_ID="${BUNDLE_ID_OVERRIDE:-$SOURCE_BUNDLE_ID}"
if [[ ! "$TARGET_BUNDLE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]]; then
    echo "Bundle ID 格式无效：$TARGET_BUNDLE_ID" >&2
    exit 2
fi
if [[ "$TARGET_BUNDLE_ID" != "$SOURCE_BUNDLE_ID" ]]; then
    /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $TARGET_BUNDLE_ID" "$APP_DIR/Info.plist"
fi

SDKROOT="$(xcrun --sdk iphoneos --show-sdk-path)"
CLANGXX="$(xcrun --sdk iphoneos --find clang++)"
PATCH_DEFINES=("-DJBRFZ_INLINE_HOOKS=1")
if [[ "$IOS_SAFE_MODE" == "1" ]]; then
    PATCH_DEFINES=(
        "-DJBRFZ_NO_INLINE_HOOKS=1"
        "-DJBRFZ_STATIC_DISPATCH_HOOKS=1"
    )
fi
if [[ "$TARGET_BUNDLE_ID" != "$SOURCE_BUNDLE_ID" ]]; then
    PATCH_DEFINES+=("-DJBRFZ_BUNDLE_ID_COMPAT=1")
fi
"$CLANGXX" \
    -arch arm64 \
    -isysroot "$SDKROOT" \
    -miphoneos-version-min=15.0 \
    -std=gnu++17 \
    -fobjc-arc \
    -fvisibility=hidden \
    -DJBRFZ_EMBEDDED=1 \
    -DJBRFZ_NO_CAPTURE=1 \
    -DJBRFZ_STATIC_AD_PATCH=1 \
    "${PATCH_DEFINES[@]}" \
    -I"$REPO_ROOT/JBRFZBypass" \
    -dynamiclib \
    -Wl,-dead_strip \
    -Wl,-install_name,"$PATCH_INSTALL_NAME" \
    "$REPO_ROOT/JBRFZBypass/Tweak.mm" \
    "$REPO_ROOT/JBRFZBypass/JBRFZPanel.mm" \
    "$DOBBY_ARCHIVE" \
    -framework Foundation \
    -framework UIKit \
    -framework CoreGraphics \
    -o "$PATCH_DYLIB"

if otool -L "$PATCH_DYLIB" | grep -Eq 'substrate|ellekit|libhooker'; then
    echo "补丁仍依赖外部 Hook 库，拒绝继续打包。" >&2
    exit 3
fi
if strings "$PATCH_DYLIB" | grep -Eq 'jbrfz_capture|REQ-RAW|GUEST_SECRET|LOGIN_MEMBER|\[CAPTURE\]'; then
    echo "补丁中仍包含请求记录代码，拒绝继续打包。" >&2
    exit 3
fi

if [[ "$MARKETPLACE_STUB" == "1" ]]; then
    # PlayCover/macOS does not provide the iOS MarketplaceKit framework used by
    # the bundled ad SDK. A real iOS device must keep the system weak link: the
    # local Swift stand-in is not pointer-authentication compatible with iOS 27.
    mkdir -p "$MARKETPLACE_FRAMEWORK/Modules/MarketplaceKit.swiftmodule"
    xcrun --sdk iphoneos swiftc \
        -target arm64-apple-ios15.0 \
        -parse-as-library \
        -emit-library \
        -emit-module \
        -emit-module-path "$MARKETPLACE_FRAMEWORK/Modules/MarketplaceKit.swiftmodule/arm64-apple-ios.swiftmodule" \
        -module-name MarketplaceKit \
        -enable-library-evolution \
        -Xlinker -install_name \
        -Xlinker "$MARKETPLACE_INSTALL_NAME" \
        "$SCRIPT_DIR/MarketplaceKitStub.swift" \
        -o "$MARKETPLACE_BINARY"
    /usr/libexec/PlistBuddy -c 'Add :CFBundleExecutable string MarketplaceKit' "$MARKETPLACE_FRAMEWORK/Info.plist"
    /usr/libexec/PlistBuddy -c 'Add :CFBundleIdentifier string com.xzd.jbrfz.marketplacekit' "$MARKETPLACE_FRAMEWORK/Info.plist"
    /usr/libexec/PlistBuddy -c 'Add :CFBundleInfoDictionaryVersion string 6.0' "$MARKETPLACE_FRAMEWORK/Info.plist"
    /usr/libexec/PlistBuddy -c 'Add :CFBundleName string MarketplaceKit' "$MARKETPLACE_FRAMEWORK/Info.plist"
    /usr/libexec/PlistBuddy -c 'Add :CFBundlePackageType string FMWK' "$MARKETPLACE_FRAMEWORK/Info.plist"
    /usr/libexec/PlistBuddy -c 'Add :CFBundleShortVersionString string 1.0' "$MARKETPLACE_FRAMEWORK/Info.plist"
    /usr/libexec/PlistBuddy -c 'Add :CFBundleVersion string 1' "$MARKETPLACE_FRAMEWORK/Info.plist"
    /usr/libexec/PlistBuddy -c 'Add :MinimumOSVersion string 15.0' "$MARKETPLACE_FRAMEWORK/Info.plist"
fi

cp "$MAIN_BINARY" "$APP_MAIN"
cp "$UNITY_BINARY" "$APP_UNITY"
cp "$PATCH_DYLIB" "$APP_DIR/Frameworks/JBRFZPatch.dylib"
chmod 0755 "$APP_MAIN" "$APP_UNITY" "$APP_DIR/Frameworks/JBRFZPatch.dylib"

# AppSealing's native protection constructors run before an injected dylib's
# constructor on macOS/PlayCover. Patch the version-locked worker entries in
# the packaged binaries so no delayed fault/exit worker can start first.
python3 "$SCRIPT_DIR/patch_arm64_rvas.py" "$APP_MAIN" \
    0x000837FC 0x000838C8 0x00083A24 0x00083A48 0x00083C24 \
    0x00083D1C 0x00083EAC 0x0008400C 0x00084108 0x0008412C
python3 "$SCRIPT_DIR/patch_arm64_rvas.py" "$APP_UNITY" \
    0x000EEC38 0x000EED04 0x000EEE60 0x000EEE84 0x000EF060 \
    0x000EF158 0x000EF2E8 0x000EF448 0x000EF544 0x000EF568
if [[ "$IOS_SAFE_MODE" == "1" ]]; then
    if [[ "$TARGET_BUNDLE_ID" != "$SOURCE_BUNDLE_ID" ]]; then
        # AppSealing 1.14 compares the native main-bundle identifier before
        # our injected dylib is initialized. For both identifier lookup
        # failure and exhausted comparison retries, enter the exact state-zero
        # path used by a successful comparison instead of storing state 3.
        python3 "$SCRIPT_DIR/patch_arm64_rvas.py" --branch-to 0x00096FB8 \
            "$APP_MAIN" \
            0x00096D30 0x00096FFC
        python3 "$SCRIPT_DIR/patch_arm64_rvas.py" --branch-to 0x001027D0 \
            "$APP_UNITY" \
            0x00102548 0x00102814
    fi
    # These three exported checks previously required Dobby. Their signed
    # static return stubs and all managed hook bridges are installed before
    # the nested framework and app signatures are generated.
    python3 "$SCRIPT_DIR/patch_arm64_rvas.py" --return-false "$APP_UNITY" \
        0x00082920 0x000831A8 0x00083338
    # Managed AppSealing wrappers previously redirected to
    # ReturnWithoutAction by Dobby. The signed build can replace them directly.
    python3 "$SCRIPT_DIR/patch_arm64_rvas.py" "$APP_UNITY" \
        0x03A1E678 0x03A1E770 0x03A1E83C 0x03A1F064
    python3 "$SCRIPT_DIR/patch_arm64_static_hooks.py" "$APP_UNITY"
fi
# Crumble.AdRemoveGameBoostCalculator.IsAdRemoveActive. This is patched on
# disk, before signing, so stock iOS never has to modify a signed __TEXT page.
python3 "$SCRIPT_DIR/patch_arm64_rvas.py" --return-true "$APP_UNITY" \
    0x03DB865C
if [[ "$MARKETPLACE_STUB" == "1" ]]; then
    python3 "$SCRIPT_DIR/redirect_weak_dylib.py" "$APP_UNITY" \
        "$MARKETPLACE_SYSTEM_PATH" "$MARKETPLACE_INSTALL_NAME"
fi
python3 "$SCRIPT_DIR/inject_load_dylib.py" "$APP_MAIN" "$PATCH_INSTALL_NAME"

/usr/libexec/PlistBuddy -c 'Delete :JBRFZPatch' "$APP_DIR/Info.plist" 2>/dev/null || true
if [[ "$IOS_SAFE_MODE" == "1" ]]; then
    PATCH_TAG='1.1.101-embedded-ios-signed-static-auto-ad-no-capture-speed3x'
    PATCH_SUMMARY='静态保护/免广告/自动功能 + 浮动面板 + Unity 3×；无运行时内联 Hook；不含请求记录'
else
    PATCH_TAG='1.1.101-embedded-static-ad-no-capture-speed3x-icall-marketplace-compat'
    PATCH_SUMMARY='静态免广告 + 面板/兼容/自动功能 + Unity 3× + MarketplaceKit Mac 兼容；不含请求记录'
fi
/usr/libexec/PlistBuddy -c "Add :JBRFZPatch string $PATCH_TAG" "$APP_DIR/Info.plist"
rm -rf "$APP_DIR/_CodeSignature"

SIGNING_MODE=adhoc
if [[ -n "${SIGN_IDENTITY:-}" || -n "${MOBILEPROVISION:-}" ]]; then
    if [[ -z "${SIGN_IDENTITY:-}" || -z "${MOBILEPROVISION:-}" ]]; then
        echo "开发证书签名必须同时提供 SIGN_IDENTITY 和 MOBILEPROVISION。" >&2
        exit 4
    fi
    if [[ ! -f "$MOBILEPROVISION" ]]; then
        echo "描述文件不存在：$MOBILEPROVISION" >&2
        exit 4
    fi
    SIGNING_MODE=development
    cp "$MOBILEPROVISION" "$APP_DIR/embedded.mobileprovision"
    security cms -D -i "$MOBILEPROVISION" > "$WORK_DIR/profile.plist"
    /usr/libexec/PlistBuddy -x -c 'Print :Entitlements' "$WORK_DIR/profile.plist" > "$WORK_DIR/entitlements.plist"
    BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP_DIR/Info.plist")"
    PROFILE_APP_ID="$(/usr/libexec/PlistBuddy -c 'Print :application-identifier' "$WORK_DIR/entitlements.plist")"
    EXPANDED_APP_ID="${PROFILE_APP_ID/\*/$BUNDLE_ID}"
    /usr/libexec/PlistBuddy -c "Set :application-identifier $EXPANDED_APP_ID" "$WORK_DIR/entitlements.plist"
    KEYCHAIN_GROUP_COUNT="$(/usr/libexec/PlistBuddy -c 'Print :keychain-access-groups' "$WORK_DIR/entitlements.plist" 2>/dev/null | grep -c '^    ' || true)"
    for ((index = 0; index < KEYCHAIN_GROUP_COUNT; index++)); do
        group="$(/usr/libexec/PlistBuddy -c "Print :keychain-access-groups:$index" "$WORK_DIR/entitlements.plist")"
        if [[ "$group" == *'*'* ]]; then
            /usr/libexec/PlistBuddy -c "Set :keychain-access-groups:$index ${group/\*/$BUNDLE_ID}" "$WORK_DIR/entitlements.plist"
        fi
    done
    find "$APP_DIR/Frameworks" -type f -name '*.dylib' -print0 | while IFS= read -r -d '' dylib; do
        codesign --force --sign "$SIGN_IDENTITY" --timestamp=none "$dylib"
    done
    find "$APP_DIR/Frameworks" -depth -type d -name '*.framework' -print0 | while IFS= read -r -d '' framework; do
        codesign --force --sign "$SIGN_IDENTITY" --timestamp=none "$framework"
    done
    codesign --force --sign "$SIGN_IDENTITY" --timestamp=none \
        --entitlements "$WORK_DIR/entitlements.plist" "$APP_DIR"
else
    codesign --force --deep --sign - --timestamp=none "$APP_DIR"
fi

codesign --verify --deep --strict --verbose=2 "$APP_DIR"
rm -f "$OUTPUT_IPA"
(cd "$WORK_DIR" && /usr/bin/zip -qry "$OUTPUT_IPA" Payload)

echo "IPA：$OUTPUT_IPA"
echo "版本：$VERSION ($BUILD)"
echo "Bundle ID：$TARGET_BUNDLE_ID"
echo "签名：$SIGNING_MODE"
echo "iOS 安全模式：$IOS_SAFE_MODE"
echo "MarketplaceKit 本地兼容：$MARKETPLACE_STUB"
echo "补丁：$PATCH_SUMMARY"
