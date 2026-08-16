; SAVA installer (Inno Setup).
;
; dp-227: every path here is RELATIVE to this file. It previously used an
; absolute path into a sibling project directory, so the installer packaged
; an older build -- none of the current work reached an installed copy.
; Absolute paths into a sibling project are how that went unnoticed for so
; long; relative paths cannot silently resolve to the wrong project.
;
; AppVersion is read from the VERSION file at the repo root -- the same
; single source core/version.py reads -- so the installer and the About
; dialog can never disagree. Edit VERSION to cut a release; nothing in this
; file needs touching.

#define VerFile FileOpen("VERSION")
#define AppVer Trim(FileRead(VerFile))
#expr FileClose(VerFile)
#if AppVer == ""
  #error VERSION file is empty or missing -- cannot determine AppVersion
#endif

[Setup]
AppName=S.A.V.A.
AppVersion={#AppVer}
AppPublisher=Odd Lux
AppPublisherURL=
DefaultDirName={autopf}\SAVA
DefaultGroupName=SAVA
OutputDir=installer_output
OutputBaseFilename=SAVA_Setup_{#AppVer}
UninstallDisplayIcon={app}\SAVA.exe
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupIconFile=assets\sava.ico
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; Built by: ./venv/Scripts/python.exe -m PyInstaller SAVA.spec
; (NOT ./venv/Scripts/pyinstaller.exe -- that shim hardcodes V1's interpreter
; path, see CLAUDE.md. Same class of V1-leftover as the paths this file used
; to carry.)
Source: "dist\SAVA\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SAVA"; Filename: "{app}\SAVA.exe"
Name: "{group}\Uninstall SAVA"; Filename: "{uninstallexe}"
Name: "{commondesktop}\SAVA"; Filename: "{app}\SAVA.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SAVA.exe"; Description: "Launch SAVA"; Flags: nowait postinstall skipifsilent
