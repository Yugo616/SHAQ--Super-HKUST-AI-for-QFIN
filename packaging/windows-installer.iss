#define AppName "SHAQ Daily Oracle Lab"
#ifndef AppVersion
  #error "AppVersion must be supplied by build_windows.ps1"
#endif

[Setup]
AppId={{15CDBE33-6E10-466F-B4F4-45022818B498}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\SHAQ Daily Oracle Lab
DefaultGroupName=SHAQ Daily Oracle Lab
OutputDir={#ProjectRoot}\dist
OutputBaseFilename=SHAQ-Daily-Oracle-Lab-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\SHAQ Daily Oracle Lab.exe

[Files]
Source: "{#ProjectRoot}\dist\desktop-windows\SHAQ Daily Oracle Lab\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\SHAQ Daily Oracle Lab"; Filename: "{app}\SHAQ Daily Oracle Lab.exe"
Name: "{autodesktop}\SHAQ Daily Oracle Lab"; Filename: "{app}\SHAQ Daily Oracle Lab.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Run]
Filename: "{app}\SHAQ Daily Oracle Lab.exe"; Description: "打开 SHAQ Daily Oracle Lab"; Flags: nowait postinstall skipifsilent

[Code]
function HasWebView2(RootKey: Integer): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(RootKey,
    'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version)
    and (Version <> '') and (Version <> '0.0.0.0');
end;

function InitializeSetup(): Boolean;
begin
  Result := HasWebView2(HKLM32) or HasWebView2(HKCU);
  if not Result then
    SuppressibleMsgBox('请先安装 Microsoft WebView2 Evergreen Runtime，再重新运行此安装包。' + #13#10 +
      '官方下载：https://developer.microsoft.com/microsoft-edge/webview2/' + #13#10 +
      '无需安装 Python、Git 或 Zipline。', mbError, MB_OK, IDOK);
end;
