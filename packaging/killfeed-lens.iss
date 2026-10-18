; Per-user installer (no admin/UAC) for the Electron app in dist\KillfeedLens-win32-x64 (app/package.mjs),
; which carries the PyInstaller onedir as its engine in resources\killfeed-lens.
; Build: iscc /DAppVersion=0.1.0 packaging\killfeed-lens.iss  ->  dist\killfeed-lens-0.1.0-setup.exe
#ifndef AppVersion
  #error Pass the version: iscc /DAppVersion=<pyproject version>
#endif

[Setup]
AppId={{D088A1B7-4DFC-486A-AEA6-829564E08877}
AppName=killfeed-lens
AppVersion={#AppVersion}
AppVerName=killfeed-lens {#AppVersion}
AppPublisher=thsvkd
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\killfeed-lens
DisableDirPage=yes
DefaultGroupName=killfeed-lens
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=killfeed-lens-{#AppVersion}-setup
SetupIconFile=killfeed-lens.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\KillfeedLens.exe
UninstallDisplayName=killfeed-lens

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Releases before the Electron UI put the Tk onedir in {app} itself; it now lives in resources\killfeed-lens.
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\kfocr.exe"

[Files]
Source: "..\dist\KillfeedLens-win32-x64\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\killfeed-lens"; Filename: "{app}\KillfeedLens.exe"
Name: "{autodesktop}\killfeed-lens"; Filename: "{app}\KillfeedLens.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\KillfeedLens.exe"; Description: "{cm:LaunchProgram,killfeed-lens}"; Flags: nowait postinstall skipifsilent
