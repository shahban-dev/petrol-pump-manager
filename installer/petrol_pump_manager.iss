; Inno Setup script for Petrol Pump Manager (offline desktop app).
;
; Build order (on the Windows build machine, from the repo root):
;   1. pyinstaller installer\petrol_pump_manager.spec   (produces dist\PetrolPumpManager\)
;   2. Compile this script with Inno Setup 6.x -> installer\Output\PetrolPumpManager-Setup-<version>.exe
;
; NOTE ON VERSION: {#MyAppVersion} is hardcoded below and must be kept in
; sync with app/config.py :: APP_VERSION (currently "1.0.0").
; The build is deliberately NOT reading the version from the Python file
; because Inno Setup compiles before any Python runs on the build machine.

#define MyAppVersion "1.0.0"

[Setup]
AppName=Petrol Pump Manager
AppVersion={#MyAppVersion}
AppVerName=Petrol Pump Manager {#MyAppVersion}
AppId={{5f063c4a-dcba-43a3-a135-4266ec7f4164}}
; ^^ AppId (GUID) 5f063c4a-dcba-43a3-a135-4266ec7f4164. This GUID must NEVER
; change. Windows uses it to identify this product; changing it makes every
; future installer look like a different product, breaking in-place upgrades
; and leaving orphaned uninstall entries.
AppPublisher=PetrolPumpManager
AppPublisherURL=
DefaultDirName={autopf}\PetrolPumpManager
DefaultGroupName=Petrol Pump Manager
OutputDir=Output
OutputBaseFilename=PetrolPumpManager-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
; Per-user install: no admin rights needed, installs into the installing
; user's own Program Files / Start Menu. The app never writes outside the
; install folder and %LOCALAPPDATA% (see app/config.py).
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
UninstallDisplayName=Petrol Pump Manager
; 64-bit only build (PySide6 wheels are 64-bit).
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
; Windows 10/11 minimum.
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
  GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Everything produced by PyInstaller (dist\PetrolPumpManager\).
; Path is relative to this script's folder (installer\).
Source: "..\dist\PetrolPumpManager\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Petrol Pump Manager"; Filename: "{app}\PetrolPumpManager.exe"
Name: "{group}\Uninstall Petrol Pump Manager"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Petrol Pump Manager"; Filename: "{app}\PetrolPumpManager.exe"; \
  Tasks: desktopicon

[Run]
Filename: "{app}\PetrolPumpManager.exe"; \
  Description: "{cm:LaunchProgram,Petrol Pump Manager}"; \
  Flags: nowait postinstall skipifsilent

; ======================================================================
; DATA SAFETY — READ BEFORE ADDING ANYTHING HERE.
; ======================================================================
; The uninstaller MUST NOT delete the business data directory
; %LOCALAPPDATA%\PetrolPumpManager (config.BACKUP_DIR / DATA_DIR in
; app/config.py). The live database, verified backups, exports and logs
; all live there, and an uninstall must never destroy the station's books.
; THEREFORE: no [UninstallDelete] entries referencing
; {localappdata}\PetrolPumpManager are permitted in this script, and no
; [Files] entry may ever target that folder. If a data-wipe is ever needed,
; it is a deliberate manual step documented in documentation/install.md
; ("Uninstall" section), never a default uninstall behaviour.
; (Intentionally no [UninstallDelete] section.)
