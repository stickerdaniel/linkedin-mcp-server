"""Contracts for the on-demand Windows election soak."""

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).parents[1]
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "election-soak.yml"


def _workflow() -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    if True in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


def test_procdump_requires_the_exact_microsoft_organization() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["soak"]["steps"]
    script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Register a postmortem debugger"
    )

    assert ".EnumerateRelativeDistinguishedNames()" in script
    assert "if ($rdn.HasMultipleElements) {\n      return $false" in script
    assert ".GetSingleElementType().Value -eq '2.5.4.10'" in script
    assert "[System.StringComparison]::Ordinal" in script
    assert "-notmatch 'O=Microsoft Corporation'" not in script
    assert "CN=Sysinternals, O=Microsoft Corporation, C=US" in script
    assert 'CN="O=Microsoft Corporation", O=Contoso Ltd, C=US' in script
    assert "CN=Tool+O=Contoso Ltd, O=Microsoft Corporation, C=US" in script
    assert "(Test-MicrosoftOrganization $multiValued)" in script

    signature_check = script.index("if ($sig.Status -ne 'Valid')")
    organization_check = script.index(
        "if (-not (Test-MicrosoftOrganization $sig.SignerCertificate.SubjectName))"
    )
    execution = script.index("& $exe -accepteula")
    assert signature_check < organization_check < execution


def test_procdump_keeps_attribution_and_trust_documentation() -> None:
    workflow_text = _WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "publisher trust, not the exact artifact" in workflow_text
    assert "Format-List FileVersion, ProductVersion" in workflow_text
    assert "Get-FileHash -Algorithm SHA256 $exe" in workflow_text
