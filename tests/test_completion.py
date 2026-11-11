"""Completion generation and real shell checks."""

from __future__ import annotations

import os
import shutil
import unittest

from tests.common import WorkspaceTest, cli, ok, run


class GeneratedCompletions(WorkspaceTest):
    def test_all_four_shells_generate(self):
        for shell in ("bash", "zsh", "fish", "powershell"):
            with self.subTest(shell=shell):
                script = ok(cli("completion", shell))
                self.assertIn("docker", script)
                self.assertIn("venv", script)
                self.assertIn("setup", script)

    @unittest.skipIf(os.name == "nt", "Bash execution is tested on Linux; Windows uses PowerShell")
    def test_bash_completion_executes(self):
        if not shutil.which("bash"):
            self.skipTest("bash unavailable")
        path = self.root / "completion.bash"
        path.write_text(ok(cli("completion", "bash")), encoding="utf-8")
        bash = r'''
          set -e
          source "$1"
          COMP_WORDS=(odoo-compose do); COMP_CWORD=1; _odoo_compose_completion
          [[ " ${COMPREPLY[*]} " == *' docker '* ]]
          COMP_WORDS=(odoo-compose se); COMP_CWORD=1; _odoo_compose_completion
          [[ " ${COMPREPLY[*]} " != *' setup '* ]]
          COMP_WORDS=(odoo-compose docker setup --cre); COMP_CWORD=3; _odoo_compose_completion
          [[ " ${COMPREPLY[*]} " == *' --create-docker-deploy '* ]]
          [[ " ${COMPREPLY[*]} " != *' --create-venv '* ]]
          COMP_WORDS=(odoo-compose venv setup --cre); COMP_CWORD=3; _odoo_compose_completion
          [[ " ${COMPREPLY[*]} " == *' --create-venv '* ]]
          [[ " ${COMPREPLY[*]} " != *' --create-docker-deploy '* ]]
          COMP_WORDS=(odoo-compose completion po); COMP_CWORD=2; _odoo_compose_completion
          [[ " ${COMPREPLY[*]} " == *' powershell '* ]]
        '''
        ok(run("bash", "-c", bash, "bash", path))

    def test_zsh_and_fish_real_shells(self):
        for shell in ("zsh", "fish"):
            if not shutil.which(shell):
                continue  # Installed in Linux CI; optional on developer machines.
            script = self.root / f"completion.{shell}"
            script.write_text(ok(cli("completion", shell)))
            ok(run(shell, "-n", script))
            if shell == "fish":
                text = ok(run("fish", "-c", f"source '{script}'; complete -C 'odoo-compose do'"))
                self.assertIn("docker", text)
                text = ok(run("fish", "-c", f"source '{script}'; complete -C 'odoo-compose venv setup --cre'"))
                self.assertIn("--create-venv", text)
                self.assertNotIn("--create-docker-deploy", text)

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_powershell_completion(self):
        # Python owns process exits; PowerShell only exercises tab completion.
        script = self.root / "completion.ps1"
        script.write_text(ok(cli("completion", "powershell")), encoding="utf-8")
        code = r'''
          . $args[0]
          $cases = @(
            @('odoo-compose do', 'docker'),
            @('odoo-compose venv se', 'setup'),
            @('odoo-compose venv setup --cre', '--create-venv'),
            @('odoo-compose docker setup --cre', '--create-docker-deploy')
          )
          foreach ($case in $cases) {
            $items = [System.Management.Automation.CommandCompletion]::CompleteInput($case[0], $case[0].Length, $null)
            $values = @($items.CompletionMatches | ForEach-Object CompletionText)
            if ($values -notcontains $case[1]) { throw "Missing '$($case[1])' from $($case[0]): $values" }
          }
        '''
        codefile = self.root / "assert-completion.ps1"
        codefile.write_text(code, encoding="utf-8")
        ok(run("pwsh", "-NoProfile", "-File", codefile, script))
