## Merge the local EPG XML with the generated guide fragments

$EPGSource = "\\ragnar\f\wwwroot\EPGStrong.xml"

$mytemp = "\\ragnar\f\Temp\mytemp.xml"
$EPG = "\\ragnar\f\wwwroot\EPG.xml"
$tempDirectory = "\\ragnar\f\Temp"
$outputDirectory = Split-Path -Parent $EPG

if (-not (Test-Path -LiteralPath $EPGSource)) {
	throw "EPG source not found: $EPGSource"
}
if (-not (Test-Path -LiteralPath $outputDirectory)) {
	throw "EPG output directory not found: $outputDirectory"
}


$listsCount = 55
for ($i = 1; $i -le $listsCount; $i++) {
	$source = "\\ragnar\f\WebGrabFiles\list$($i)\guide.xml"
	$dest = Join-Path $tempDirectory "temp$($i).xml"
	if (Test-Path -LiteralPath $source) {
		Get-Content $source | Select-Object -SkipLast 1 | Set-Content $mytemp -Encoding UTF8
		Get-Content $mytemp | Select-Object -Skip 2 | Set-Content $dest -Encoding UTF8
		Remove-Item $mytemp -ErrorAction SilentlyContinue
	}
}

$localDocument = New-Object System.Xml.XmlDocument
$localDocument.PreserveWhitespace = $true
$localDocument.Load($EPGSource)

$outputDocument = New-Object System.Xml.XmlDocument
$xmlDeclaration = $outputDocument.CreateXmlDeclaration("1.0", "UTF-8", $null)
$outputDocument.AppendChild($xmlDeclaration) | Out-Null

$localRoot = $localDocument.DocumentElement
$outputRoot = $outputDocument.CreateElement("tv")
foreach ($attribute in $localRoot.Attributes) {
	$outputRoot.SetAttribute($attribute.Name, $attribute.Value)
}
$outputDocument.AppendChild($outputRoot) | Out-Null

$channelIds = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::Ordinal)
$programmeKeys = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::Ordinal)

function Add-EpgNode {
	param(
		[System.Xml.XmlElement]$Node,
		[System.Xml.XmlElement]$TargetRoot,
		[System.Xml.XmlDocument]$TargetDocument,
		[System.Collections.Generic.HashSet[string]]$ChannelIds,
		[System.Collections.Generic.HashSet[string]]$ProgrammeKeys
	)

	if ($Node.Name -eq "channel") {
		$channelId = $Node.GetAttribute("id")
		if ($channelId -and $ChannelIds.Add($channelId)) {
			$TargetRoot.AppendChild($TargetDocument.ImportNode($Node, $true)) | Out-Null
		}
		return
	}

	if ($Node.Name -eq "programme") {
		$channelId = $Node.GetAttribute("channel")
		$start = $Node.GetAttribute("start")
		$stop = $Node.GetAttribute("stop")
		$titleNode = $Node.SelectSingleNode("title")
		$title = if ($titleNode) { $titleNode.InnerText } else { "" }
		$key = "$channelId|$start|$stop|$title"

		if ($ProgrammeKeys.Add($key)) {
			$TargetRoot.AppendChild($TargetDocument.ImportNode($Node, $true)) | Out-Null
		}
	}
}

foreach ($node in $localRoot.ChildNodes) {
	if ($node -is [System.Xml.XmlElement]) {
		Add-EpgNode -Node $node -TargetRoot $outputRoot -TargetDocument $outputDocument `
			-ChannelIds $channelIds -ProgrammeKeys $programmeKeys
	}
}

$fragmentFiles = @(Get-ChildItem -LiteralPath $tempDirectory -Filter "temp*.xml" -File -ErrorAction SilentlyContinue |
	Sort-Object Name)

foreach ($fragmentFile in $fragmentFiles) {
	$fragmentText = [System.IO.File]::ReadAllText($fragmentFile.FullName)
	if ([string]::IsNullOrWhiteSpace($fragmentText)) {
		continue
	}

	$fragmentDocument = New-Object System.Xml.XmlDocument
	$fragmentDocument.PreserveWhitespace = $true
	try {
		$fragmentDocument.LoadXml("<root>$fragmentText</root>")
	}
	catch {
		Write-Warning "Skipping invalid EPG fragment: $($fragmentFile.FullName): $($_.Exception.Message)"
		continue
	}

	foreach ($node in $fragmentDocument.DocumentElement.ChildNodes) {
		if ($node -is [System.Xml.XmlElement]) {
			Add-EpgNode -Node $node -TargetRoot $outputRoot -TargetDocument $outputDocument `
				-ChannelIds $channelIds -ProgrammeKeys $programmeKeys
		}
	}
}

$settings = New-Object System.Xml.XmlWriterSettings
$settings.Encoding = New-Object System.Text.UTF8Encoding($false)
$settings.Indent = $false
$settings.OmitXmlDeclaration = $false
$writer = [System.Xml.XmlWriter]::Create($EPG, $settings)
try {
	$outputDocument.Save($writer)
}
finally {
	$writer.Dispose()
}