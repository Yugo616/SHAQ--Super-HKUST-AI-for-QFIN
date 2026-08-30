#define AppName "SHAQ Daily Oracle"
#define AppVersion "0.2.0-internal"

[Setup]
AppId={{15CDBE33-6E10-466F-B4F4-45022818B498}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\SHAQ Daily Oracle
DefaultGroupName=SHAQ Daily Oracle
OutputDir={#ProjectRoot}\dist
OutputBaseFilename=SHAQ-Daily-Oracle-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\SHAQ Daily Oracle.exe

[Files]
Source: "{#ProjectRoot}\dist\desktop-windows\SHAQ Daily Oracle\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\SHAQ Daily Oracle"; Filename: "{app}\SHAQ Daily Oracle.exe"
Name: "{autodesktop}\SHAQ Daily Oracle"; Filename: "{app}\SHAQ Daily Oracle.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Run]
Filename: "{app}\SHAQ Daily Oracle.exe"; Description: "打开 SHAQ Daily Oracle"; Flags: nowait postinstall skipifsilent
