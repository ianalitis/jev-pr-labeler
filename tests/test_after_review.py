import argparse
import io
import json
import unittest
from unittest.mock import Mock, patch

from jev_labeler import __main__ as cli
from jev_labeler.after_review import dispatch, main


@patch.dict('os.environ', {'GH_TOKEN': 'fake-github'}, clear=True)
class BatchTests(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(repo='owner/repo', pr=None, check_sha=None,
                                       all_open=True, threshold=.75, apply=False,
                                       ensure_labels=False, require_greptile=True)
        self.github = Mock()
        self.github.pages.return_value = [{'number': number} for number in (1, 2, 3)]
        self.first = {'pull_request': 1, 'status': 'waiting_for_greptile'}
        self.last = {'pull_request': 3, 'mode': 'dry-run', 'add': [], 'remove': []}

    def test_known_failures_preserve_other_pr_outcomes(self):
        for failure in (KeyError('PRIVATE'), TypeError('PRIVATE'), OSError('PRIVATE'),
                        RuntimeError('API connection failed.'), ValueError('Invalid provider usage')):
            with self.subTest(error=type(failure).__name__), \
                    patch('jev_labeler.after_review.GitHub', return_value=self.github), \
                    patch('jev_labeler.after_review.run', side_effect=[self.first, failure, self.last]) as run:
                report = dispatch(self.args)
                self.assertEqual(report['matched_open_prs'], 3)
                self.assertEqual(report['results'][0], self.first)
                self.assertEqual(report['results'][2], self.last)
                error = report['results'][1]
                self.assertEqual(error['pull_request'], 2)
                self.assertEqual(error['status'], 'error')
                self.assertNotIn('PRIVATE', error['reason'])
                if isinstance(failure, (ValueError, RuntimeError)):
                    self.assertEqual(error['reason'], str(failure))
                self.assertEqual([call.args[0].pr for call in run.call_args_list], [1, 2, 3])
                self.assertIsNone(self.args.pr)

    def test_main_emits_complete_json_and_fails_when_one_pr_errors(self):
        closed = {'head': {'sha': 'a' * 40}, 'state': 'closed'}
        self.github.pull.side_effect = [closed, {}, closed]
        with patch('sys.argv', ['after_review', '--repo', 'owner/repo', '--all-open']), \
                patch('jev_labeler.after_review.GitHub', return_value=self.github), \
                patch('jev_labeler.__main__.GitHub', return_value=self.github), \
                patch('sys.stdout', new_callable=io.StringIO) as output, \
                patch('sys.stderr', new_callable=io.StringIO) as errors:
            self.assertEqual(main(), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(len(report['results']), 3)
        self.assertEqual([item['status'] for item in report['results']],
                         ['skipped_closed', 'error', 'skipped_closed'])
        self.assertEqual(report['results'][2]['pull_request'], 3)
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertEqual(errors.getvalue(), '')

    def test_empty_batch_succeeds_without_classification(self):
        self.github.pages.return_value = []
        with patch('sys.argv', ['after_review', '--repo', 'owner/repo', '--all-open']), \
                patch('jev_labeler.after_review.GitHub', return_value=self.github), \
                patch('jev_labeler.after_review.run') as run, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue())['results'], [])
        run.assert_not_called()

    def test_unexpected_programming_errors_are_not_swallowed(self):
        with patch('jev_labeler.after_review.GitHub', return_value=self.github), \
                patch('jev_labeler.after_review.run', side_effect=AssertionError('bug')):
            with self.assertRaises(AssertionError):
                dispatch(self.args)

    def test_single_pr_cli_keeps_sanitized_errors(self):
        for failure in (KeyError('PRIVATE'), TypeError('PRIVATE'), OSError('PRIVATE')):
            with self.subTest(error=type(failure).__name__), \
                    patch('sys.argv', ['jev-pr-labeler', '--repo', 'owner/repo', '--pr', '1']), \
                    patch('jev_labeler.__main__.run', side_effect=failure), \
                    patch('sys.stderr', new_callable=io.StringIO) as errors:
                self.assertEqual(cli.main(), 1)
                self.assertNotIn('PRIVATE', errors.getvalue())
                self.assertTrue(errors.getvalue().startswith('jev-pr-labeler: '))
