#pragma once

#ifdef __cplusplus
extern "C" {
#endif

// Minimal declaration used by the embedded IPA build. The implementation is
// linked statically, so the resulting patch dylib has no external hook-library
// dependency.
int DobbyHook(void *address, void *replacement, void **original);

#ifdef __cplusplus
}
#endif
