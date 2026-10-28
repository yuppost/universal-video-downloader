; Inno Setup script. Build:  iscc /DAppVersion=1.0.0 installer\UniversalVideoStudio.iss   (after build.bat)
; Per-user install (no administrator rights): the app folder stays writable, so the one-click updater works.
; FFmpeg and Deno are NOT part of the installer: the app downloads them on first launch.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6F1D2A52-3B7E-4C57-9E0D-5B1A7C0E4D21}
AppName=Universal Video Studio
AppVersion={#AppVersion}
AppPublisher=Universal Video Studio
DefaultDirName={localappdata}\Programs\UniversalVideoStudio
DefaultGroupName=Universal Video Studio
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\installer_out
OutputBaseFilename=UniversalVideoStudio-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\UniversalVideoStudio.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\UniversalVideoStudio\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Universal Video Studio"; Filename: "{app}\UniversalVideoStudio.exe"
Name: "{autodesktop}\Universal Video Studio"; Filename: "{app}\UniversalVideoStudio.exe"; Tasks: desktopicon

; Browser extension (Chrome / Edge / Brave): the native host (uvs-native-host.exe) and the extension folder are part of
; the app folder; the host is registered per user and the app refreshes this on every start.
[Registry]
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.universalvideostudio.host"; ValueType: string; ValueData: "{userappdata}\UniversalVideoStudio\native\com.universalvideostudio.host.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\com.universalvideostudio.host"; ValueType: string; ValueData: "{userappdata}\UniversalVideoStudio\native\com.universalvideostudio.host.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\com.universalvideostudio.host"; ValueType: string; ValueData: "{userappdata}\UniversalVideoStudio\native\com.universalvideostudio.host.json"; Flags: uninsdeletekey

[Run]
Filename: "{app}\UniversalVideoStudio.exe"; Description: "Start Universal Video Studio"; Flags: nowait postinstall skipifsilent
