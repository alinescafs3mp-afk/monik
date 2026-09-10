"""Typed source projections retain unknowns instead of inventing measurements."""
import unittest
from monik.model import normalize, rate_windows


class NormalizedFields(unittest.TestCase):
    def test_invalid_percentage_types_are_unknown(self):
        for raw in (True, False, [], {}, '30', float('inf'), float('nan'), 10**400):
            with self.subTest(raw_type=type(raw).__name__):
                item=rate_windows({'primary':{'used_percent':raw}})[0]
                self.assertFalse(item['valid'])
                self.assertIsNone(item['used_percent'])
                self.assertIsNone(item['remaining_percent'])

    def test_percent_zero_and_hundred_are_valid(self):
        for used,remaining in ((0,100),(100,0),(23.5,76.5)):
            item=rate_windows({'primary':{'used_percent':used}})[0]
            self.assertTrue(item['valid'])
            self.assertEqual(item['remaining_percent'],remaining)

    def test_invalid_window_and_reset_types_are_unknown(self):
        for raw in (True, [], {}, 'invalid'):
            item=rate_windows({'primary':{'used_percent':5,'window_minutes':raw,'resets_at':raw}})[0]
            self.assertIsNone(item['window_minutes'])
            self.assertIsNone(item['resets_at'])
            self.assertEqual(item['remaining_percent'],95)

    def test_named_fields_survive_both_source_styles(self):
        a=rate_windows({'limit_id':'codex','primary':{'used_percent':10,'window_minutes':300,'resets_at':1700000000}})[0]
        b=rate_windows({'limitId':'codex','primary':{'usedPercent':10,'windowDurationMins':300,'resetsAt':1700000000}})[0]
        for key in ('limit_id','used_percent','remaining_percent','window_minutes','resets_at','valid'):
            self.assertEqual(a[key],b[key])

    def test_malformed_limit_identity_does_not_drop_following_record(self):
        for ident in ([],{},True):
            item=rate_windows({'limit_id':ident,'primary':{'used_percent':10}})[0]
            self.assertEqual(item['limit_id'],'unknown')

    def test_settings_are_text_or_unknown(self):
        for raw in (True,42,[],{'unexpected':1}):
            row={'type':'turn_context','payload':dict.fromkeys(('model','effort','reasoning_effort','service_tier'),raw)}
            result=normalize(row,'astra','root','source')[0]
            for key in ('model','effort','reasoning_effort','service_tier'):
                self.assertIsNone(result['data'][key])
            self.assertIsNone(result['model'])

    def test_technical_settings_are_not_translated(self):
        row={'type':'turn_context','payload':{'model':'gpt-6-astra','effort':'ultra','service_tier':'default'}}
        data=normalize(row,'astra','root','source')[0]['data']
        self.assertEqual((data['model'],data['effort'],data['service_tier']),('gpt-6-astra','ultra','default'))

    def test_nonstring_response_identity_is_not_counted_as_usage(self):
        for ident in (True,45,['id'],{'id':1}):
            row={'type':'token_usage_record','payload':{'response_id':ident,'usage':{'input_tokens':1,'output_tokens':2,'total_tokens':3}}}
            self.assertEqual(normalize(row,'astra','root','source')[0]['kind'],'unsupported_usage')

if __name__=='__main__': unittest.main()
