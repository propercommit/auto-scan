; Inno Setup script for Auto Scan Windows installer
; Requires Inno Setup 6+ (https://jrsoftware.org/isinfo.php)
;
; Build: ISCC.exe /DAppVersion=0.1.0 installer.iss

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppName=Auto Scan
AppVersion={#AppVersion}
AppPublisher=Auto Scan
AppPublisherURL=https://github.com/propercommit/auto-scan
DefaultDirName={autopf}\Auto Scan
DefaultGroupName=Auto Scan
OutputDir=..\dist
OutputBaseFilename=AutoScan-{#AppVersion}-Setup
SetupIconFile=icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\Auto Scan\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Auto Scan"; Filename: "{app}\Auto Scan.exe"
Name: "{group}\Uninstall Auto Scan"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Auto Scan"; Filename: "{app}\Auto Scan.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Auto Scan.exe"; Description: "Launch Auto Scan"; Flags: nowait postinstall skipifsilent
