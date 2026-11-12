; Windows installer for the Discord music bot. Built by build.ps1, which passes:
;   /DAppVersion=1.0.0  /DStageDir=<folder with the files to install>  /O<output folder>

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Discord Music Bot"
#define DataDir "{commonappdata}\Discord Music Bot"

[Setup]
AppId={{6B1E8D2A-4C7F-4E39-9A51-D3F0B8C2E741}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=discord-music-bot-{#AppVersion}-windows-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
CloseApplications=no
UninstallDisplayName={#AppName}
SetupLogging=yes

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{autoprograms}\{#AppName}\Set bot token"; Filename: "{app}\discord-music-bot.cmd"; Parameters: "token"
Name: "{autoprograms}\{#AppName}\View live log"; Filename: "{app}\discord-music-bot.cmd"; Parameters: "logs"
Name: "{autoprograms}\{#AppName}\Bot status"; Filename: "{app}\discord-music-bot.cmd"; Parameters: "status"
Name: "{autoprograms}\{#AppName}\Restart bot"; Filename: "{app}\discord-music-bot.cmd"; Parameters: "restart"

[Registry]
; Put "discord-music-bot" on PATH (removed again on uninstall, see [Code]).
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; ValueType: expandsz; \
  ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NotOnPath(ExpandConstant('{app}'))

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\manage.ps1"" uninstall-cleanup"; \
  Flags: runhidden waituntilterminated; RunOnceId: "StopBot"

[UninstallDelete]
; Created by install.ps1, not by the installer itself.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\ffmpeg"
Type: filesandordirs; Name: "{app}\deno"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
var
  TokenPage: TInputQueryWizardPage;

function NotOnPath(Dir: string): Boolean;
var
  Path: string;
begin
  if not RegQueryStringValue(HKLM, 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'Path', Path) then
    Path := '';
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Path) + ';') = 0;
end;

function TokenAlreadySet: Boolean;
var
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := False;
  if LoadStringsFromFile(ExpandConstant('{#DataDir}\bot.env'), Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
      if (Pos('DISCORD_TOKEN=', Lines[I]) = 1) and (Length(Trim(Lines[I])) > 64) then
        Result := True;
end;

procedure InitializeWizard;
begin
  TokenPage := CreateInputQueryPage(wpSelectDir, 'Discord bot token',
    'The bot needs its token to log in to Discord.',
    'Get it from https://discord.com/developers/applications -> your app -> Bot -> Reset Token.' + #13#10 +
    'You can leave this empty and set it later from Start menu -> Discord Music Bot -> Set bot token.');
  TokenPage.Add('Bot token:', True);
  TokenPage.Add('Server ID (optional, makes new slash commands appear instantly):', False);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = TokenPage.ID) and TokenAlreadySet;  // upgrading: keep the existing token
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = TokenPage.ID) and (Trim(TokenPage.Values[0]) <> '') and (Length(Trim(TokenPage.Values[0])) < 50) then
  begin
    MsgBox('That doesn''t look like a bot token (too short). Paste the whole token, or leave it empty for now.', mbError, MB_OK);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  // Upgrading: stop the running bot so its files can be replaced.
  if FileExists(ExpandConstant('{app}\manage.ps1')) then
    Exec('powershell.exe', '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\manage.ps1') + '" uninstall-cleanup',
      '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  TokenFile, Args: string;
  Code: Integer;
begin
  if CurStep <> ssPostInstall then Exit;
  Args := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\install.ps1') + '"';
  if (not TokenAlreadySet) and (Trim(TokenPage.Values[0]) <> '') then
  begin
    // Handed over in a file inside Setup's private temp folder, which install.ps1 deletes after reading.
    TokenFile := ExpandConstant('{tmp}\token.txt');
    SaveStringToFile(TokenFile, Trim(TokenPage.Values[0]) + #13#10 + Trim(TokenPage.Values[1]) + #13#10, False);
    Args := Args + ' -TokenFile "' + TokenFile + '"';
  end;
  WizardForm.StatusLabel.Caption := 'Downloading Python, FFmpeg and Deno and setting up the bot (a few minutes)...';
  WizardForm.ProgressGauge.Style := npbstMarquee;
  // Hidden: in a visible console, one click pauses the whole setup (Windows' QuickEdit mode). Output goes to install.log.
  if not Exec('powershell.exe', Args, '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
    MsgBox('Setting up the bot failed. See the log:' + #13#10 + ExpandConstant('{#DataDir}\install.log') + #13#10#13#10 +
      'You can run this installer again to retry.', mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Path, Dir: string;
  P: Integer;
begin
  if CurUninstallStep <> usPostUninstall then Exit;
  // Take the install folder off PATH.
  Dir := ExpandConstant('{app}');
  if RegQueryStringValue(HKLM, 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'Path', Path) then
  begin
    P := Pos(';' + Uppercase(Dir), Uppercase(Path));
    if P > 0 then
    begin
      Delete(Path, P, Length(Dir) + 1);
      RegWriteExpandStringValue(HKLM, 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'Path', Path);
    end;
  end;
  if DirExists(ExpandConstant('{#DataDir}')) and (not UninstallSilent) and
     (MsgBox('Also delete the bot token, saved playlists and logs?' + #13#10 +
             'Choose No to keep them for a later reinstall.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
    DelTree(ExpandConstant('{#DataDir}'), True, True, True);
end;
