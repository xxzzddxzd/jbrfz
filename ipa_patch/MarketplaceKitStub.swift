// Minimal MarketplaceKit compatibility surface for PlayCover/macOS.
//
// CookieRunCrumble 1.1.101's bundled ad SDK weak-links MarketplaceKit, but
// still asks Swift to resolve AppDistributor metadata when the framework is
// absent from the iOS-on-macOS runtime. Keep this declaration ABI-compatible
// with the public API used by the SDK and report the normal App Store route.

public enum AppDistributor: Sendable {
    case appStore
    case testFlight
    case marketplace(String)
    case web
    case other

    public static var current: AppDistributor {
        get async {
            .appStore
        }
    }
}
