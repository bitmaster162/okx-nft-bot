from pathlib import Path

from okx_nft_bot.registry import CollectionRegistry


def test_registry_loads_active_collection(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(
        '{"collections":[{"name":"alpha","collection_address":"0xabc","enabled":true,"source_modes":["trades","listings"]},{"name":"off","collection_address":"0xdef","enabled":false}]}'
    )
    registry = CollectionRegistry.from_path(path)
    assert registry.names() == ["alpha"]
    alpha = registry.get("alpha")
    assert alpha is not None
    assert alpha.source_modes == ("trades", "listings")
