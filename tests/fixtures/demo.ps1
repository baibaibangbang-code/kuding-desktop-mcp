param([Parameter(Mandatory=$true)][string]$ResultPath)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Kuding Desktop MCP Demo'
$form.ClientSize = New-Object System.Drawing.Size(620,310)
$form.StartPosition = 'CenterScreen'
$form.BackColor = [System.Drawing.Color]::White
$form.Font = New-Object System.Drawing.Font('Segoe UI',12)
$heading = New-Object System.Windows.Forms.Label
$heading.Text = 'Kuding Desktop MCP'
$heading.Font = New-Object System.Drawing.Font('Segoe UI',20,[System.Drawing.FontStyle]::Bold)
$heading.SetBounds(28,24,570,42)
$note = New-Object System.Windows.Forms.Label
$note.Text = 'Synthetic test window - no account or business data'
$note.SetBounds(30,76,555,30)
$box = New-Object System.Windows.Forms.TextBox
$box.Name = 'DemoInput'
$box.AccessibleName = 'Demo input'
$box.SetBounds(30,126,555,36)
$button = New-Object System.Windows.Forms.Button
$button.Text = 'Verify'
$button.AccessibleName = 'Verify'
$button.SetBounds(30,178,130,42)
$status = New-Object System.Windows.Forms.Label
$status.Name = 'VerificationStatus'
$status.AccessibleName = 'Verification status'
$status.SetBounds(30,240,565,46)
$status.Text = 'Waiting for the MCP verification step.'
$button.Add_Click({
  if ($box.Text -eq 'Kuding Desktop MCP test') {
    $status.Text = 'Verified locally: Kuding Desktop MCP test'
    $status.ForeColor = [System.Drawing.Color]::FromArgb(24,120,75)
    [IO.File]::WriteAllText($ResultPath,'{"verified":true,"text":"Kuding Desktop MCP test"}')
  } else {
    $status.Text = 'The input does not match the synthetic test text.'
  }
})
$form.Controls.AddRange(@($heading,$note,$box,$button,$status))
$form.Add_Shown({$form.Activate()})
[void]$form.ShowDialog()
