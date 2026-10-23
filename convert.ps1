if ($args.Count -ne 2) {
    [Console]::Error.WriteLine("Usage: .\convert.ps1 projectFolder partName")
    exit 2
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    [Console]::Error.WriteLine("Python was not found on PATH.")
    exit 127
}

$scriptPath = Join-Path $PSScriptRoot "project_maker.py"
& python $scriptPath $args[0] $args[1]
exit $LASTEXITCODE
