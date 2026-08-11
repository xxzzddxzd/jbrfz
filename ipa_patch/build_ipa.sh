#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_IPA="${SOURCE_IPA:-$REPO_ROOT/10101/1.zip}"
MAIN_BINARY="${MAIN_BINARY:-$REPO_ROOT/10101/main/CookieRunCrumble}"
UNITY_BINARY="${UNITY_BINARY:-$REPO_ROOT/10101/UnityFramework}"
OUTPUT_IPA="${OUTPUT_IPA:-$SCRIPT_DIR/dist/CookieRunCrumble-1.0.101-JBRFZ.ipa}"
PATCH_INSTALL_NAME="@executable_path/Frameworks/JBRFZPatch.dylib"

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

if [[ ! -d "$APP_DIR" || ! -f "$APP_UNITY" ]]; then
    echo "源归档不是预期的 CookieRunCrumble 1.0.101 IPA。" >&2
    exit 2
fi

VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP_DIR/Info.plist")"
BUILD="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP_DIR/Info.plist")"
if [[ "$VERSION" != "1.0.101" || "$BUILD" != "19" ]]; then
    echo "版本不匹配：需要 1.0.101 (19)，实际 $VERSION ($BUILD)。" >&2
    exit 2
fi

SDKROOT="$(xcrun --sdk iphoneos --show-sdk-path)"
CLANGXX="$(xcrun --sdk iphoneos --find clang++)"
"$CLANGXX" \
    -arch arm64 \
    -isysroot "$SDKROOT" \
    -miphoneos-version-min=15.0 \
    -std=gnu++17 \
    -fobjc-arc \
    -fvisibility=hidden \
    -DJBRFZ_EMBEDDED=1 \
    -DJBRFZ_NO_CAPTURE=1 \
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
python3 "$SCRIPT_DIR/inject_load_dylib.py" "$APP_MAIN" "$PATCH_INSTALL_NAME"

/usr/libexec/PlistBuddy -c 'Delete :JBRFZPatch' "$APP_DIR/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c 'Add :JBRFZPatch string 1.0.101-embedded-no-capture-speed3x-icall' "$APP_DIR/Info.plist"
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
echo "签名：$SIGNING_MODE"
echo "补丁：面板/兼容/自动功能 + Unity 3×；不含请求记录"
