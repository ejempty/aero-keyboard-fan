# Builds KeyboardLight.exe (self-contained installer + app).
# Requires: Python 3, and `pip install hid pystray pillow wmi pywin32 pyinstaller`.

python -m PyInstaller --noconfirm --onefile --windowed `
  --name KeyboardLight --icon feather.ico `
  --add-data "feather.png;." `
  --add-data "feather.ico;." `
  --add-data "aero_fan.mof;." `
  --collect-all hid `
  --hidden-import wmi --hidden-import win32com.client --hidden-import win32timezone `
  keyboard_light.pyw

Write-Host "Built dist\KeyboardLight.exe"
