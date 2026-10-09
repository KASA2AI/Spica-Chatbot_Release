// Windows 10 ConPTY transport. This file is copied to protected storage before
// SYSTEM tasks use it; ordinary application calls run their copy unprivileged.
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using Microsoft.Win32.SafeHandles;

public static class WindowsRgbNative
{
    [StructLayout(LayoutKind.Sequential)] struct Coord { public short X, Y; }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct Startup {
        public int cb; public string reserved, desktop, title;
        public int x,y,width,height,xChars,yChars,fill,flags; public short show,reservedSize;
        public IntPtr reservedBytes,input,output,error;
    }
    [StructLayout(LayoutKind.Sequential)] struct StartupEx { public Startup startup; public IntPtr attributes; }
    [StructLayout(LayoutKind.Sequential)] struct ProcessInfo { public IntPtr process,thread; public uint pid,tid; }
    [StructLayout(LayoutKind.Sequential)] struct Limits {
        public long processTime,jobTime; public uint flags; public UIntPtr min,max;
        public uint active; public UIntPtr affinity; public uint priority,scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct Counters { public ulong a,b,c,d,e,f; }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits {
        public Limits basic; public Counters io; public UIntPtr processMemory,jobMemory,peakProcess,peakJob;
    }
    [StructLayout(LayoutKind.Sequential)] struct ServiceStatus { public uint type,state,controls,winError,serviceError,checkpoint,hint; }
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr OpenSCManager(string machine,string database,uint access);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr OpenService(IntPtr manager,string name,uint access);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool ControlService(IntPtr service,uint control,out ServiceStatus status);
    [DllImport("advapi32.dll")] static extern bool CloseServiceHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool CreatePipe(out IntPtr read,out IntPtr write,IntPtr attributes,uint size);
    [DllImport("kernel32.dll")] static extern int CreatePseudoConsole(Coord size,IntPtr input,IntPtr output,uint flags,out IntPtr console);
    [DllImport("kernel32.dll")] static extern void ClosePseudoConsole(IntPtr console);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool InitializeProcThreadAttributeList(IntPtr list,int count,int flags,ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool UpdateProcThreadAttribute(IntPtr list,uint flags,IntPtr attribute,IntPtr value,IntPtr size,IntPtr previous,IntPtr returned);
    [DllImport("kernel32.dll")] static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool CreateProcess(string application,StringBuilder command,IntPtr processSecurity,IntPtr threadSecurity,bool inherit,uint flags,IntPtr environment,string directory,ref StartupEx startup,out ProcessInfo process);
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr attributes,string name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int kind,ref ExtendedLimits info,uint length);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateJobObject(IntPtr job,uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateProcess(IntPtr process,uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint WaitForSingleObject(IntPtr handle,uint milliseconds);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr process,out uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);

    public sealed class Result { public int ExitCode; public string Output; }
    static void Check(bool success) { if(!success) throw new Win32Exception(Marshal.GetLastWin32Error()); }
    static string Literal(string text) { return "'"+text.Replace("'","''")+"'"; }
    static string Argument(string text) {
        var value=new StringBuilder("\""); int slashes=0;
        foreach(char ch in text) {
            if(ch=='\\') { slashes++; continue; }
            value.Append('\\',ch=='"' ? slashes*2+1 : slashes); slashes=0; value.Append(ch);
        }
        return value.Append('\\',slashes*2).Append('"').ToString();
    }
    public static void StopLightingService() {
        IntPtr manager=OpenSCManager(null,null,1); Check(manager!=IntPtr.Zero);
        IntPtr service=IntPtr.Zero;
        try {
            service=OpenService(manager,"LightingService",0x24); Check(service!=IntPtr.Zero);
            ServiceStatus status;
            if(!ControlService(service,1,out status) && Marshal.GetLastWin32Error()!=1062)
                throw new Win32Exception(Marshal.GetLastWin32Error());
        } finally { if(service!=IntPtr.Zero) CloseServiceHandle(service); CloseServiceHandle(manager); }
    }
    public static Result Run(string executable,string[] arguments,int timeoutMs) {
        if(timeoutMs<1 || timeoutMs>30000) throw new ArgumentOutOfRangeException("timeoutMs");
        var joined=new StringBuilder(); foreach(string argument in arguments) { if(joined.Length>0) joined.Append(' '); joined.Append(Argument(argument)); }
        string source="$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; $p=Start-Process -FilePath "+Literal(executable)+
            " -ArgumentList "+Literal(joined.ToString())+" -NoNewWindow -Wait -PassThru; exit $p.ExitCode";
        string powershell=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),"WindowsPowerShell/v1.0/powershell.exe");
        string command=Argument(powershell)+" -NoProfile -NonInteractive -EncodedCommand "+Convert.ToBase64String(Encoding.Unicode.GetBytes(source));
        IntPtr inputRead=IntPtr.Zero,inputWrite=IntPtr.Zero,outputRead=IntPtr.Zero,outputWrite=IntPtr.Zero;
        IntPtr console=IntPtr.Zero,attributes=IntPtr.Zero,job=IntPtr.Zero; bool attributesReady=false,assigned=false;
        ProcessInfo child=new ProcessInfo(); Task<string> reader=null; bool truncated=false;
        try {
            Check(CreatePipe(out inputRead,out inputWrite,IntPtr.Zero,0));
            Check(CreatePipe(out outputRead,out outputWrite,IntPtr.Zero,0));
            int hr=CreatePseudoConsole(new Coord { X=240,Y=120 },inputRead,outputWrite,0,out console);
            if(hr!=0) Marshal.ThrowExceptionForHR(hr);
            CloseHandle(inputRead); inputRead=IntPtr.Zero; CloseHandle(outputWrite); outputWrite=IntPtr.Zero;
            var stream=new FileStream(new SafeFileHandle(outputRead,true),FileAccess.Read,4096,false); outputRead=IntPtr.Zero;
            reader=Task.Factory.StartNew(() => {
                using(stream) using(var text=new StreamReader(stream,new UTF8Encoding(false,false))) {
                    var buffer=new char[4096]; var result=new StringBuilder(); int count;
                    while((count=text.Read(buffer,0,buffer.Length))>0) {
                        if(result.Length+count<=2097152) result.Append(buffer,0,count); else truncated=true;
                    }
                    return result.ToString();
                }
            });
            IntPtr size=IntPtr.Zero; InitializeProcThreadAttributeList(IntPtr.Zero,1,0,ref size);
            attributes=Marshal.AllocHGlobal(size); Check(InitializeProcThreadAttributeList(attributes,1,0,ref size)); attributesReady=true;
            Check(UpdateProcThreadAttribute(attributes,0,new IntPtr(0x20016),console,new IntPtr(IntPtr.Size),IntPtr.Zero,IntPtr.Zero));
            job=CreateJobObject(IntPtr.Zero,null); Check(job!=IntPtr.Zero);
            var limits=new ExtendedLimits(); limits.basic.flags=0x2000;
            Check(SetInformationJobObject(job,9,ref limits,(uint)Marshal.SizeOf(typeof(ExtendedLimits))));
            var startup=new StartupEx(); startup.startup.cb=Marshal.SizeOf(typeof(StartupEx)); startup.attributes=attributes;
            Check(CreateProcess(powershell,new StringBuilder(command),IntPtr.Zero,IntPtr.Zero,false,0x80004,IntPtr.Zero,
                                Path.GetDirectoryName(executable),ref startup,out child));
            Check(AssignProcessToJobObject(job,child.process)); assigned=true;
            if(ResumeThread(child.thread)==UInt32.MaxValue) throw new Win32Exception(Marshal.GetLastWin32Error());
            uint wait=WaitForSingleObject(child.process,(uint)timeoutMs);
            if(wait!=0) { TerminateJobObject(job,1); throw new TimeoutException("OpenRGB console command did not finish"); }
            uint exit; Check(GetExitCodeProcess(child.process,out exit));
            // The console shim waited for OpenRGB. Any unexpected descendants
            // must still be gone before success can be returned.
            Check(TerminateJobObject(job,exit));
            ClosePseudoConsole(console); console=IntPtr.Zero;
            if(!reader.Wait(2000)) throw new TimeoutException("OpenRGB console output did not close");
            if(truncated) throw new IOException("OpenRGB console output exceeded its limit");
            return new Result { ExitCode=unchecked((int)exit),Output=reader.Result };
        } finally {
            if(child.process!=IntPtr.Zero && !assigned) TerminateProcess(child.process,1);
            if(job!=IntPtr.Zero) { TerminateJobObject(job,1); CloseHandle(job); }
            if(child.thread!=IntPtr.Zero) CloseHandle(child.thread);
            if(child.process!=IntPtr.Zero) CloseHandle(child.process);
            if(console!=IntPtr.Zero) ClosePseudoConsole(console);
            if(attributesReady) DeleteProcThreadAttributeList(attributes);
            if(attributes!=IntPtr.Zero) Marshal.FreeHGlobal(attributes);
            foreach(IntPtr handle in new [] { inputRead,inputWrite,outputRead,outputWrite }) if(handle!=IntPtr.Zero) CloseHandle(handle);
        }
    }
}
