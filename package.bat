set PLUGIN=plugin.audio.pandora
set VERSION=2.1.10

set DIR=C:\dev\Kodi\%PLUGIN%
set ZIPPER="C:\Program Files\7-Zip\7z.exe"

%ZIPPER% a .\%PLUGIN%-%VERSION%.zip   %DIR% -xr!.venv 