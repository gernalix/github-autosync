import json
import tempfile
import unittest
from pathlib import Path

import autosync_core
from activity_journal import (
    append_activity, _normalize_activity_event,
    _load_activity_data_cursor, _save_activity_data_cursor,
)
from autosync_errors import AutosyncError


class ActivityJournalBoundaryTests(unittest.TestCase):
    def test_legacy_imports_still_share_identical_functions(self):
        for name in ('append_activity','_normalize_activity_event',
                     '_load_activity_data_cursor','_save_activity_data_cursor'):
            self.assertIs(getattr(autosync_core,name), globals()[name])
        self.assertIs(autosync_core.AutosyncError, AutosyncError)

    def test_cursor_and_event_validation_are_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)
            event=append_activity(state,action='fetch',repo='gernalix/test')
            raw=(state/'activity.jsonl').read_text().splitlines()[0]
            normalized=_normalize_activity_event(json.loads(raw),line_number=0,raw_line=raw)
            self.assertEqual(normalized['event_id'],event['event_id'])
            self.assertEqual(_load_activity_data_cursor(state,1),0)
            _save_activity_data_cursor(state,1)
            self.assertEqual(_load_activity_data_cursor(state,1),1)
            with self.assertRaises(AutosyncError):
                _load_activity_data_cursor(state,0)


if __name__=='__main__':
    unittest.main()
