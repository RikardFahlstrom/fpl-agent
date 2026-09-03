import unittest
from types import SimpleNamespace

from fpl_agent import tools
from fpl_agent.models import TransfersData
from fpl_agent.state import store


class _PreseasonClient:
    user_info = {"player": {"entry": 431892}}

    async def get_my_team(self, entry_id: int) -> dict:
        self.entry_id = entry_id
        return {
            "picks": [
                {
                    "element": player_id,
                    "position": player_id,
                    "is_captain": player_id == 1,
                    "is_vice_captain": player_id == 2,
                    "selling_price": None,
                    "purchase_price": 45 + player_id,
                }
                for player_id in range(1, 16)
            ],
            "transfers": {
                "bank": None,
                "value": None,
                "limit": None,
                "made": None,
                "cost": None,
            },
            "chips": None,
        }

    async def get_players(self) -> list:
        return [
            SimpleNamespace(
                id=player_id,
                web_name=f"Player {player_id}",
                team_name="Test FC",
            )
            for player_id in range(1, 16)
        ]


class ManagerSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def test_transfer_schema_accepts_preseason_nulls(self) -> None:
        transfers = TransfersData(
            bank=None,
            value=None,
            limit=None,
            made=None,
            cost=None,
            status=None,
        )

        self.assertIsNone(transfers.limit)
        self.assertIsNone(transfers.bank)

    async def test_preseason_nullable_transfer_state_is_preserved(self) -> None:
        session_id = "preseason-nullable-regression"
        previous_active_session_id = tools.get_active_session()
        previous_client = store.active_sessions.get(session_id)
        client = _PreseasonClient()
        store.active_sessions[session_id] = client
        tools.set_active_session(session_id)

        try:
            snapshot = await tools.get_manager_snapshot()
        finally:
            tools.set_active_session(previous_active_session_id)
            if previous_client is None:
                store.active_sessions.pop(session_id, None)
            else:
                store.active_sessions[session_id] = previous_client

        self.assertEqual(client.entry_id, 431892)
        self.assertEqual(snapshot["status"], "connected")
        self.assertEqual(len(snapshot["picks"]), 15)
        self.assertEqual(snapshot["picks"][0]["purchase_price"], 46)
        self.assertEqual(snapshot["picks"][0]["selling_price"], 46)
        self.assertIsNone(snapshot["bank"])
        self.assertIsNone(snapshot["squad_value"])
        self.assertIsNone(snapshot["free_transfers"])
        self.assertIsNone(snapshot["transfer_cost"])
        self.assertEqual(snapshot["chips"], [])

    async def test_authenticated_schema_diagnostic_is_redacted(self) -> None:
        session_id = "schema-diagnostic-regression"
        previous_active_session_id = tools.get_active_session()
        previous_client = store.active_sessions.get(session_id)
        client = _PreseasonClient()
        store.active_sessions[session_id] = client
        tools.set_active_session(session_id)

        try:
            diagnostic = await tools.get_authenticated_schema_diagnostics()
        finally:
            tools.set_active_session(previous_active_session_id)
            if previous_client is None:
                store.active_sessions.pop(session_id, None)
            else:
                store.active_sessions[session_id] = previous_client

        my_team = diagnostic["endpoints"]["/api/my-team/{entry_id}/"]
        self.assertTrue(diagnostic["redacted"])
        self.assertIn("limit", my_team["transfers"]["null_fields"])
        self.assertEqual(my_team["picks"]["count"], 15)
        self.assertNotIn("431892", str(diagnostic))
        self.assertNotIn("Player 1", str(diagnostic))


if __name__ == "__main__":
    unittest.main()
