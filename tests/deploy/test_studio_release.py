from unittest.mock import Mock

import pytest

from deploy.docker import studio_release as release

SHA = "1" * 40
TAG = "v0.1.0-rc.1"


def api_fixture(annotated=False):
    return {
        f"releases/tags/{TAG}": {"tag_name": TAG, "draft": False, "published_at": "2026-01-01"},
        f"git/ref/tags/{TAG}": {
            "ref": f"refs/tags/{TAG}",
            "object": {
                "type": "tag" if annotated else "commit",
                "sha": "2" * 40 if annotated else SHA,
            },
        },
        "git/tags/" + "2" * 40: {"object": {"type": "commit", "sha": SHA}},
    }


@pytest.mark.parametrize("annotated", [True, False])
def test_release_tag_must_resolve_to_expected_commit(annotated):
    release.verify_pin(TAG, SHA, api_fixture(annotated).__getitem__)


@pytest.mark.parametrize(
    "case", ["draft", "absent", "unpublished", "moved", "branch", "tree", "cycle"]
)
def test_invalid_release_is_rejected(case):
    objects = api_fixture(True)
    metadata = objects[f"releases/tags/{TAG}"]
    ref = objects[f"git/ref/tags/{TAG}"]
    if case == "draft":
        metadata["draft"] = True
    elif case == "absent":
        del objects[f"releases/tags/{TAG}"]
    elif case == "unpublished":
        metadata["published_at"] = None
    elif case == "moved":
        ref["object"] = {"type": "commit", "sha": "3" * 40}
    elif case == "branch":
        ref["ref"] = "refs/heads/" + TAG
    elif case == "tree":
        ref["object"]["type"] = "tree"
    else:
        objects["git/tags/" + "2" * 40]["object"] = ref["object"]
    with pytest.raises((ValueError, KeyError)):
        release.verify_pin(TAG, SHA, objects.__getitem__)


def test_invalid_input_never_calls_api():
    api = Mock()
    for tag, sha in [("../main", SHA), ("--help", SHA), (TAG, "HEAD")]:
        with pytest.raises(ValueError):
            release.verify_pin(tag, sha, api)
    api.assert_not_called()


@pytest.mark.parametrize("visibility", ["public", "internal", None])
def test_private_package_required(visibility):
    with pytest.raises(ValueError):
        release.verify_registry(
            {"visibility": visibility, "name": "omnigent-studio", "package_type": "container"}
        )


def test_existing_private_registry_is_allowed():
    release.verify_registry(
        {"name": "omnigent-studio", "package_type": "container", "visibility": "private"}
    )


def test_wheel_inventory_is_exact(tmp_path):
    for name in release.DISTRIBUTIONS:
        (tmp_path / f"{name.replace('-', '_')}-0.1.0-py3-none-any.whl").touch()
    release.verify_wheels(tmp_path)
    (tmp_path / "omnigent-0.2.0-py3-none-any.whl").touch()
    with pytest.raises(ValueError):
        release.verify_wheels(tmp_path)
    for file in tmp_path.glob("omnigent-*.whl"):
        file.unlink()
    with pytest.raises(ValueError):
        release.verify_wheels(tmp_path)
