$ErrorActionPreference = "Stop"

Add-Type -ReferencedAssemblies @("System", "Microsoft.CSharp") -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public class IsoSaver
{
    public static void WriteIso(object streamObj, long totalBytes, string isoPath)
    {
        IStream stream = (IStream)streamObj;
        byte[] buffer = new byte[8 * 1024 * 1024];
        IntPtr pRead = Marshal.AllocHGlobal(4);
        try
        {
            using (FileStream fs = new FileStream(isoPath, FileMode.Create, FileAccess.Write))
            {
                long written = 0;
                while (written < totalBytes)
                {
                    stream.Read(buffer, buffer.Length, pRead);
                    int read = Marshal.ReadInt32(pRead);
                    if (read == 0) break;
                    fs.Write(buffer, 0, read);
                    written += read;
                }
                Console.WriteLine("written bytes: " + written);
            }
        }
        finally
        {
            Marshal.FreeHGlobal(pRead);
        }
    }
}
'@

$stage = "C:\Users\lx\Downloads\PaddleOCR-VL\win-app\iso-staging-v10"
$iso = "C:\Users\lx\Downloads\PaddleOCR-VL\win-app\release_v12\PaddleOCR-VL-Desktop-0.1.0.iso"
if (Test-Path $iso) { Remove-Item $iso -Force }

$fsi = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
$fsi.FileSystemsToCreate = 4
$fsi.UDFRevision = 0x0250
$fsi.VolumeName = "PaddleOCR-VL Desktop"
$fsi.FreeMediaBlocks = 6400000
$fsi.Root.AddTree($stage, $false) | Out-Null

$result = $fsi.CreateResultImage()
$totalBytes = $result.TotalBlocks * $result.BlockSize
Write-Output ("TotalBytes: " + $totalBytes)

[IsoSaver]::WriteIso($result.ImageStream, $totalBytes, $iso)

Get-Item $iso | Select-Object FullName, @{N='GB';E={[math]::Round($_.Length/1GB,3)}}, LastWriteTime
