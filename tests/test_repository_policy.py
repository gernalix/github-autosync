"""No-shell policy checks for the autosync repository allowlist."""
import unittest

import autosync_core
import repository_policy


class RepositoryPolicyTest(unittest.TestCase):
    def test_compatibility_exports_share_one_policy(self):
        self.assertIs(autosync_core.is_allowed_repo, repository_policy.is_allowed_repo)
        self.assertIs(autosync_core.ALLOWED_REPOSITORIES, repository_policy.ALLOWED_REPOSITORIES)

    def test_allowlist_is_exact_and_normalizes_known_remotes(self):
        self.assertTrue(repository_policy.is_allowed_repo("gernalix", "MegaVault"))
        self.assertFalse(repository_policy.is_allowed_repo("someone", "MegaVault"))
        self.assertEqual(
            repository_policy.github_remote_key("git@github.com:gernalix/MegaVault.git"),
            "gernalix/MegaVault",
        )
        self.assertIsNone(repository_policy.github_remote_key("https://not-github.example/gernalix/MegaVault"))
        self.assertTrue(repository_policy.is_independent_canonical_writer_repo("gernalix", "codex-usage"))

    def test_fingerprint_ignores_equivalent_remote_notation(self):
        base = dict(url="git@github.com:gernalix/MegaVault.git", default_branch="master", pushed_at="now")
        equivalent = dict(base, url="https://github.com/gernalix/MegaVault")
        self.assertEqual(repository_policy.repo_fingerprint(base), repository_policy.repo_fingerprint(equivalent))


if __name__ == "__main__":
    unittest.main()
