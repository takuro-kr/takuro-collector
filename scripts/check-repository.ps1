$ErrorActionPreference = 'Stop'

$forbiddenNames = @(
    'collector.db', '*.sqlite', '*.sqlite3', '.env', '.env.*',
    '*cookie*', '*credential*', '*secret*', 'storage-state*.json',
    '*.exe', '*.zip', '*.pdf', '*.csv', '*.tsv',
    '*.jpg', '*.jpeg', '*.png', '*.webp'
)

$tracked = @(git ls-files)
if ($LASTEXITCODE -ne 0) { throw 'git ls-files failed' }

$bad = foreach ($file in $tracked) {
    foreach ($pattern in $forbiddenNames) {
        if ((Split-Path $file -Leaf) -like $pattern) { $file; break }
    }
}

if ($bad) {
    $bad | Sort-Object -Unique | ForEach-Object { Write-Error "Forbidden tracked file: $_" }
    exit 1
}

$secretPattern = '(?i)(api[_-]?key|password|passwd|authorization|collector[_-]?key)\s*[:=]\s*["''][A-Za-z0-9_./+=-]{24,}["'']'
$textExtensions = @('.py', '.json', '.toml', '.yaml', '.yml', '.env')
$hits = foreach ($file in $tracked) {
    if ([IO.Path]::GetExtension($file) -in $textExtensions -and (Test-Path -LiteralPath $file)) {
        $content = Get-Content -LiteralPath $file -Raw
        if ($content -match $secretPattern) { $file }
    }
}
if ($hits) {
    $hits | ForEach-Object { Write-Error "Possible embedded secret: $_" }
    exit 1
}

Write-Host "Repository hygiene OK ($($tracked.Count) tracked files checked)."
