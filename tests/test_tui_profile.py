"""Profile display regression: exercise the existing diagnostic unchanged."""
import copy
import unittest
from diagnostics.check_tui_profile import check
from monik.tui import filter_overview


class TerminalProfile(unittest.TestCase):
    def test_original_diagnostic_passes_without_changing_its_contract(self):
        result=check()
        self.assertTrue(result['ok'])
        self.assertTrue(all(result['checks'].values()))

    def test_filter_does_not_mutate_server_data(self):
        data={'profiles':[{'profile':'astra'},{'profile':'sol'}],'cursor':71}
        original=copy.deepcopy(data)
        filtered=filter_overview('overview',data,{'profile':'sol'})
        self.assertEqual(filtered['profiles'],[{'profile':'sol'}])
        self.assertEqual(filtered['cursor'],71)
        self.assertEqual(data,original)

    def test_no_filter_retains_all_cards(self):
        data={'profiles':[{'profile':'astra'},{'profile':'sol'}]}
        self.assertIs(filter_overview('overview',data,{}),data)
        self.assertIs(filter_overview('overview',data,{'profile':''}),data)

    def test_other_pages_are_not_reinterpreted_as_overview(self):
        data={'items':[{'profile':'sol'}]}
        self.assertIs(filter_overview('activity',data,{'profile':'sol'}),data)

if __name__=='__main__': unittest.main()
