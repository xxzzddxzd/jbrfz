THEOS_PACKAGE_SCHEME = rootless
TARGET := iphone:clang:latest:16.0
ARCHS := arm64

include $(THEOS)/makefiles/common.mk

TWEAK_NAME := JBRFZBypass

JBRFZBypass_FILES := Tweak.mm JBRFZPanel.mm
JBRFZBypass_CFLAGS := -fobjc-arc -Wall -Wextra
JBRFZBypass_FRAMEWORKS := Foundation UIKit CoreGraphics
JBRFZBypass_LIBRARIES := substrate

INSTALL_TARGET_PROCESSES := CookieRunCrumble

include $(THEOS_MAKE_PATH)/tweak.mk
