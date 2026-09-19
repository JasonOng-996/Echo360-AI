#define MyAppName "EchoLecture AI"
#define MyAppVersion "2.1.0"
#define MyAppExeName "EchoLectureAI.exe"
[Setup]
AppId={{B9D2A49D-0A77-4B77-8EA6-A2F378546A82}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\EchoLecture AI
DefaultGroupName=EchoLecture AI
OutputDir=release
OutputBaseFilename=EchoLectureAI-Setup-2.1.0
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
WizardStyle=modern
[Files]
Source: "dist\EchoLectureAI\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{autoprograms}\EchoLecture AI"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\EchoLecture AI"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch EchoLecture AI"; Flags: nowait postinstall skipifsilent
