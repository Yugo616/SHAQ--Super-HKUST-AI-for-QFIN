import unittest

from shaq_daily_oracle.research_collection import _document_text


class FilingDocumentTests(unittest.TestCase):
    def test_packaged_smoke_exercises_filing_dependencies_and_policy(self):
        from shaq_daily_oracle.lab_smoke import filing_input_smoke
        from pathlib import Path
        self.assertTrue(filing_input_smoke(Path(__file__).resolve().parents[1]))

    def test_nested_hidden_metadata_and_head_are_removed(self):
        source = b'<html><head><meta name="x" content="hidden"/></head><body><ix:hidden><div><span>Revenue 9999</span></div></ix:hidden><p>Revenue 120</p></body></html>'
        text = _document_text(source, 2000)
        self.assertIn('Revenue 120', text)
        self.assertNotIn('9999', text)

    def test_risk_and_performance_both_retrieved_with_exact_values(self):
        source=('<p>Revenue growth was 12.70% with sales of $1,234.56 million.</p>' * 200+
                '<h2>Liquidity risk</h2><p>Debt covenant breach creates $987.65 million repayment risk.</p>').encode()
        text=_document_text(source,2000)
        self.assertIn('12.70%',text)
        self.assertIn('$987.65 million',text)
        self.assertIn('$1,234.56 million',text)

    def test_full_archive_and_excerpt_links_are_consistent(self):
        from shaq_daily_oracle.filing_documents import document_policy,parse_document,select_documents
        import hashlib
        source=b'<p>Revenue grew 9%.</p><p>Guidance cut by 5%.</p>'
        policy=document_policy()
        doc=parse_document(source,source_uri='https://www.sec.gov/example.htm',policy=policy)
        view=select_documents([doc],maximum_characters=2000,policy=policy)[0]
        self.assertEqual(view['document_coverage']['raw_sha256'],hashlib.sha256(source).hexdigest())
        self.assertTrue(view['document_coverage']['complete_document'])
        self.assertEqual([r['block_id'] for r in view['document_coverage']['references']],['b000000','b000001'])
        self.assertIn('Guidance cut by 5%',view['document_text'])

    def test_large_table_splits_between_rows_and_repeats_original_headers(self):
        from shaq_daily_oracle.filing_documents import document_policy,parse_document,select_documents
        source=('<table><tr><th>Metric</th><th>USD</th></tr>'+ '<tr><td>Revenue</td><td>100</td></tr>'*1000+'</table>').encode()
        policy={**document_policy(), 'block_characters':400}
        doc=parse_document(source,source_uri='',policy=policy)
        view=select_documents([doc],maximum_characters=500,policy=policy)[0]
        self.assertFalse(view['document_coverage']['complete_document'])
        self.assertGreater(len(doc['blocks']),1)
        self.assertTrue(all('Metric,USD' in b['text'] for b in doc['blocks']))
        self.assertTrue(all('Revenue,100' in b['text'] for b in doc['blocks']))
        self.assertTrue(view['document_text'])
        ranges=[b['table_row_range'] for b in doc['blocks']]
        self.assertEqual(ranges[0][0],1)
        self.assertEqual(ranges[-1][1],1001)
        self.assertTrue(all(left[1]==right[0] for left,right in zip(ranges,ranges[1:])))

    def test_merged_cells_do_not_repeat_entire_table_metadata_in_each_chunk(self):
        from shaq_daily_oracle.filing_documents import document_policy, parse_document
        source=('<h2>USD millions</h2><table><tr><th>Metric</th><th>2026</th></tr>'+
                '<tr><td colspan="2">Revenue (123.40)</td></tr>'*200+'</table>').encode()
        doc=parse_document(source,source_uri='',policy={**document_policy(),'block_characters':400})
        self.assertLess(doc['normalized_characters'],len(source)*2)
        self.assertTrue(all(len(b['text']) < 600 for b in doc['blocks']))
        self.assertTrue(any(b.get('merged_cells') for b in doc['blocks']))

    def test_pdf_is_not_silently_treated_as_html(self):
        with self.assertRaises(ValueError):
            _document_text(b'%PDF-1.7 binary content',2000)

    def test_guidance_at_end_survives_long_filing_instead_of_prefix_truncation(self):
        # The previous [:maximum_characters] discards the only changed outlook.
        source = ('<html><h1>Quarterly report</h1>' +
                  '<p>Administrative information and company background.</p>' * 1000 +
                  '<h2>Outlook and risks</h2><p>Fiscal 2027 revenue guidance reduced to '
                  '$18.2 billion because of cancellations.</p></html>').encode()
        text = _document_text(source, 1600)
        self.assertIn('$18.2 billion', text)
        self.assertIn('cancellations', text)
        self.assertLessEqual(len(text), 1600)

    def test_table_row_keeps_cells_together_with_period_and_units(self):
        source = b'<h2>USD millions</h2><table><tr><th>Metric</th><th>2026</th><th>2025</th></tr><tr><td>Revenue</td><td>120</td><td>90</td></tr></table>'
        text = _document_text(source, 2000)
        self.assertIn('Revenue,120,90', text)
        self.assertIn('Metric,2026,2025', text)
        self.assertIn('USD millions', text)

    def test_hidden_inline_xbrl_metadata_does_not_become_visible_evidence(self):
        source = b'<html><ix:hidden>Revenue 9999</ix:hidden><p>Revenue 120</p><script>secret</script></html>'
        text = _document_text(source, 2000)
        self.assertIn('Revenue 120', text)
        self.assertNotIn('9999', text)
        self.assertNotIn('secret', text)
