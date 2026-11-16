"""Linear board extension manifest."""

from omnigent.extensions import (
    EXTENSION_API_VERSION,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPermission,
    PageContribution,
    PrimaryNavigationContribution,
)


def get_manifest() -> ExtensionManifest:
    return ExtensionManifest(
        id="egzakta.linear",
        display_name="Linear",
        distribution="omnigent-linear-extension",
        version="0.1.0",
        requires_omnigent=">=0.22.0.dev0,<1",
        extension_api=EXTENSION_API_VERSION,
        entrypoints=ExtensionEntrypoints(
            browser="dist/extension.js", browser_css="dist/extension.css"
        ),
        permissions=frozenset(
            {
                ExtensionPermission.LINEAR_READ,
                ExtensionPermission.NAVIGATION,
                ExtensionPermission.PROJECTS_READ,
                ExtensionPermission.PROJECTS_WRITE,
            }
        ),
        pages=(PageContribution("egzakta.linear.board", "Linear", "board", "board"),),
        primary_navigation=(
            PrimaryNavigationContribution(
                "egzakta.linear.navigation", "Linear", "egzakta.linear.board", icon="list"
            ),
        ),
    )
