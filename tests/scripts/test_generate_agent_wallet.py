"""Agent Wallet 生成スクリプトのテスト。

ロジック（純関数 generate_agent_wallet）の検証のみ。
実際の鍵は捨てる（テストではコンソール出力もしない）。
"""

from __future__ import annotations

from eth_account import Account

from scripts.generate_agent_wallet import generate_agent_wallet


def test_generates_valid_address() -> None:
    address, _ = generate_agent_wallet()
    assert address.startswith("0x")
    assert len(address) == 42  # 0x + 40 hex chars


def test_generates_valid_private_key() -> None:
    _, private_key = generate_agent_wallet()
    assert private_key.startswith("0x")
    assert len(private_key) == 66  # 0x + 64 hex chars


def test_generates_unique_pairs() -> None:
    # 暗号学的乱数なので衝突する確率は実質ゼロ。
    addr1, key1 = generate_agent_wallet()
    addr2, key2 = generate_agent_wallet()
    assert addr1 != addr2
    assert key1 != key2


def test_address_derived_from_private_key() -> None:
    # 公開鍵からチェックサム付きアドレスへの導出が一貫していることを確認。
    address, private_key = generate_agent_wallet()
    derived = Account.from_key(private_key).address
    assert derived == address
