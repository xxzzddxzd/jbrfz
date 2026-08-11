#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// Master switch for guide auto-claim / auto-actions.
// Default: OFF (safe for new/guest accounts).
bool JbrfzAutoFeaturesEnabled(void);

// Fixed 3x Unity time scale for the 1.0.101 binary.
bool JbrfzUnitySpeedEnabled(void);
void JbrfzSetUnitySpeedEnabled(bool enabled);

// Cancel delayed auto actions when master switch turns off.
// Implemented in Tweak.mm.
void JbrfzCancelAutoActions(const char *reason);

// Install floating plugin panel (main thread).
void JbrfzStartPluginPanel(void);

#ifdef __cplusplus
}
#endif
