; GalCoplit Windows 安装脚本（Inno Setup 6）
; 由 build.py --install / build_install.bat 调用 ISCC 编译。
;
; 覆盖安装策略：
; - 只发布程序文件（exe / _internal / worker / icon）
; - 不打包 config.json、records.db、paddle_ocr_cpu、paddle_ocr_gpu
; - 因此升级覆盖时这些用户配置/数据/组件会留在原目录不动
; - 卸载时 Inno 也只删自己装过的文件，同样不会清用户数据

#define MyAppName "GalCoplit"
#define MyAppVersion "2.0"
#define MyAppPublisher "INDEXxiaoxia"
#define MyAppExeName "GalCoplit.exe"
#define MyAppURL "https://github.com/INDEXxiaoxia/GalCoplit"

[Setup]
AppId={{A7C0PL1T-GALC-4F01-9E2A-INDEXXIAOXIA01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 现代样式默认跳过欢迎页。强制：欢迎 → 选目录 → 确认 → 安装
DisableWelcomePage=no
DisableDirPage=no
DisableReadyPage=no
AllowNoIcons=yes
OutputDir=dist
OutputBaseFilename=GalCoplit-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UsePreviousAppDir=yes
UsePreviousGroup=yes
CloseApplications=yes
RestartApplications=no
; 允许覆盖安装到同一目录；不触碰未列入 [Files] 的用户文件
DirExistsWarning=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "installer\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: checkedonce

[Files]
; 程序目录版产物。切勿把用户数据打进安装包。
Source: "dist\GalCoplit-install\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 GalCoplit"; Flags: nowait postinstall skipifsilent
