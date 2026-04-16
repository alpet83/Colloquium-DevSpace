param(
    [string]$Title = "Confirm action",
    [string]$Message = "Approve operation?",
    [string]$YesText = "Yes",
    [string]$NoText = "No"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing

    $form = New-Object System.Windows.Forms.Form
    $form.Text = $Title
    $form.StartPosition = "CenterScreen"
    $form.TopMost = $true
    $form.Width = 520
    $form.Height = 190
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false

    $label = New-Object System.Windows.Forms.Label
    $label.Left = 16
    $label.Top = 18
    $label.Width = 480
    $label.Height = 70
    $label.Text = $Message
    $label.AutoSize = $false
    $form.Controls.Add($label)

    $yes = New-Object System.Windows.Forms.Button
    $yes.Text = $YesText
    $yes.Width = 110
    $yes.Height = 32
    $yes.Left = 280
    $yes.Top = 98
    $yes.DialogResult = [System.Windows.Forms.DialogResult]::Yes
    $form.Controls.Add($yes)

    $no = New-Object System.Windows.Forms.Button
    $no.Text = $NoText
    $no.Width = 110
    $no.Height = 32
    $no.Left = 396
    $no.Top = 98
    $no.DialogResult = [System.Windows.Forms.DialogResult]::No
    $form.Controls.Add($no)

    $form.AcceptButton = $yes
    $form.CancelButton = $no

    $result = $form.ShowDialog()
    if ($result -eq [System.Windows.Forms.DialogResult]::Yes) {
        Write-Output "approved"
        exit 0
    }

    Write-Output "rejected"
    exit 1
}
catch {
    Write-Error $_
    exit 2
}
