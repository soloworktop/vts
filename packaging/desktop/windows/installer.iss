#define AppId "io.github.soloworktop.vts"
[Setup]
AppId={#AppId}
AppName=VTS
AppVersion={#DisplayVersion}
VersionInfoVersion={#NumericVersion}
AppPublisher=VTS contributors
SetupIconFile={#SourcePath}\..\icon.ico
DefaultDirName={localappdata}\Programs\VTS
DefaultGroupName=VTS
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=vts-{#DisplayVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\VTS.exe
[Files]
Source: "{#BuildRoot}\VTS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\VTS"; Filename: "{app}\VTS.exe"
Name: "{userdesktop}\VTS"; Filename: "{app}\VTS.exe"; Tasks: desktopicon
[Tasks]
Name: desktopicon; Description: "Create desktop shortcut"; Flags: unchecked
[Run]
Filename: "{app}\VTS.exe"; Description: "Launch VTS"; Flags: nowait postinstall skipifsilent
[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
// User data is deliberately outside {app}; uninstall does not remove it.
