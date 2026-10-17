; CC Session Sync Windows installer - Inno Setup 6.5+  (structure ported from CC Msg's installer\CCMsg.iss)
; Build: tools\build_desktop.ps1 calls  ISCC /DAppVersion=x.y.z /DSourceDir=<dist\CCSessionSync> /DOutputDir=<dist> installer\CCSessionSync.iss
; - per-user install to %LOCALAPPDATA%\Programs\CCSessionSync ({userpf}), no admin / UAC
; - Start menu shortcut, optional desktop shortcut, uninstaller registered under the current user
; - same AppId => upgrades in place; a running copy (maybe hidden in the tray) is asked to quit first (CCSessionSync.exe --quit)
; - files still in use (an after-quit helper waiting for Claude / Codex to exit) are renamed out of the way instead of blocking
; - silent update from the app (only after the user clicked 立即更新):
;     Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /NORESTART /RELAUNCH=1 [/RELAUNCHARGS="--port N"]
; - user data (~\.cc-sessionsync) is never touched by install / uninstall

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\CCSessionSync"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
; /DTestBuild=1 builds an isolated "CC Session Sync TEST" (own AppId, folder, mutex, tray, autostart value) for tests
#ifdef TestBuild
  #define MyAppId "{{3B8E2D41-5C7A-4E19-A0F2-7E57C5513C0D}"
  #define MyName "CC Session Sync TEST"
  #define MyDir "CCSessionSyncTEST"
  #define MyMutex "Local\CCSessionSyncDesktop-TEST"
  #define MyRun "CC Session Sync TEST"
  #define MyOut "CCSessionSync-TEST-Setup-"
#else
  #define MyAppId "{{9A4C7E15-2B3D-4F68-8C1E-5D2A9B7E3F41}"
  #define MyName "CC Session Sync"
  #define MyDir "CCSessionSync"
  #define MyMutex "Local\CCSessionSyncDesktop"
  #define MyRun "CC Session Sync"
  #define MyOut "CCSessionSync-Setup-"
#endif

[Setup]
AppId={#MyAppId}
AppName={#MyName}
AppVersion={#AppVersion}
AppVerName={#MyName} {#AppVersion}
AppPublisher=helefee
AppPublisherURL=https://github.com/helefee/CC-SessionSync
AppSupportURL=https://github.com/helefee/CC-SessionSync/issues
AppUpdatesURL=https://github.com/helefee/CC-SessionSync/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={userpf}\{#MyDir}
DefaultGroupName={#MyName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#MyOut}{#AppVersion}
SetupIconFile=..\cc_sessionsync\icon.ico
UninstallDisplayIcon={app}\CCSessionSync.exe
UninstallDisplayName={#MyName} {#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=CCSessionSync.exe
RestartApplications=no
SetupMutex=CCSessionSyncSetupMutex{#MyDir}
ShowLanguageDialog=no
LanguageDetectionMethod=none

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl,CCSessionSync.zh.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{cm:AppTitle}"; Filename: "{app}\CCSessionSync.exe"; WorkingDir: "{app}"
Name: "{autoprograms}\{cm:Uninstall}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{cm:AppTitle}"; Filename: "{app}\CCSessionSync.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\CCSessionSync.exe"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\CCSessionSync.exe"; Parameters: "{param:RELAUNCHARGS}"; Flags: nowait; Check: ShouldRelaunch

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function ShouldRelaunch: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

{ wait up to ~20 s for a running CC Session Sync window (it holds this mutex) to quit by itself }
procedure WaitForApp;
var
  i: Integer;
begin
  i := 0;
  while CheckForMutexes('{#MyMutex}') and (i < 80) do
  begin
    Sleep(250);
    i := i + 1;
  end;
end;

{ move every old program file out of the way: delete it, or if it is in use rename it
  (Windows allows renaming a running exe / loaded dll); leftovers go on the next upgrade / uninstall }
procedure ClearDir(const Dir: String);
var
  FR: TFindRec;
  P: String;
begin
  if FindFirst(Dir + '\*', FR) then
  try
    repeat
      if (FR.Name <> '.') and (FR.Name <> '..') then
      begin
        P := Dir + '\' + FR.Name;
        if FR.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0 then
          ClearDir(P)
        else if (CompareText(Copy(FR.Name, 1, 5), 'unins') <> 0) then
        begin
          if not DeleteFile(P) then
            RenameFile(P, P + '.old-' + GetDateTimeString('yyyymmddhhnnsszzz', #0, #0));
        end;
      end;
    until not FindNext(FR);
  finally
    FindClose(FR);
  end;
  RemoveDir(Dir);
end;

{ the app may be hidden in the tray (closing the window only hides it): CCSessionSync.exe --quit posts a quit
  message to its tray window and waits for its mutex to go away }
procedure QuitRunningApp;
var
  rc: Integer;
begin
  if CheckForMutexes('{#MyMutex}') and FileExists(ExpandConstant('{app}\CCSessionSync.exe')) then
    Exec(ExpandConstant('{app}\CCSessionSync.exe'), '--quit', '', SW_HIDE, ewWaitUntilTerminated, rc);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    QuitRunningApp;
    WaitForApp;
    if DirExists(ExpandConstant('{app}')) then
      ClearDir(ExpandConstant('{app}'));
  end;
#ifdef TestBuild
  if CurStep = ssPostInstall then
    SaveStringToFile(ExpandConstant('{app}\instance.txt'), 'TEST', False);
#endif
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    QuitRunningApp;
    WaitForApp;
    { autostart entry written by the app (tray menu / settings): remove it with the app }
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#MyRun}');
  end;
end;
