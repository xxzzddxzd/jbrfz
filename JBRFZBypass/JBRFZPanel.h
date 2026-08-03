#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// Master switch for guide auto-claim / auto-actions.
// Default: OFF (safe for new/guest accounts).
bool JbrfzAutoFeaturesEnabled(void);

// Cancel delayed auto actions when master switch turns off.
// Implemented in Tweak.mm.
void JbrfzCancelAutoActions(const char *reason);

// Install floating plugin panel (main thread).
void JbrfzStartPluginPanel(void);

#ifdef __cplusplus
}
#endif
