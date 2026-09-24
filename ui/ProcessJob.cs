using System;
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace HermesSetup
{
    // Owns the worker's whole process tree: powershell -> install.ps1 -> uv/python/npm/node/git.
    // A Job Object, not `taskkill /T`: taskkill walks parent PIDs and misses a grandchild whose
    // parent already exited (npm and install.ps1 both leave such orphans); a job holds every
    // descendant no matter who exited first. With killOnClose the tree also dies when the
    // installer window itself crashes, because Windows closes our handle then.
    //
    // BREAKAWAY_OK (never SILENT_BREAKAWAY_OK): only a process that explicitly asks with
    // CREATE_BREAKAWAY_FROM_JOB leaves the job. The Hermes gateway daemon does exactly that
    // (hermes_cli/gateway_windows.py _spawn_detached) and must outlive the installer; without
    // the flag it would fall back to staying inside the job and die with the window.
    public sealed class ProcessJob : IDisposable
    {
        const int JobObjectExtendedLimitInformation = 9;
        const uint LimitBreakawayOk = 0x00000800;
        const uint LimitKillOnJobClose = 0x00002000;

        [StructLayout(LayoutKind.Sequential)]
        struct BasicLimits
        {
            public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass, SchedulingClass;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct IoCounters { public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes; }
        [StructLayout(LayoutKind.Sequential)]
        struct ExtendedLimits
        {
            public BasicLimits Basic;
            public IoCounters Io;
            public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits info, int length);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool TerminateJobObject(IntPtr job, uint exitCode);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool CloseHandle(IntPtr handle);

        IntPtr handle;
        int rootPid;
        public bool Assigned { get; private set; }

        public ProcessJob(bool killOnClose)
        {
            handle = CreateJobObject(IntPtr.Zero, null);
            if (handle != IntPtr.Zero && !SetLimits(LimitBreakawayOk | (killOnClose ? LimitKillOnJobClose : 0))) { CloseHandle(handle); handle = IntPtr.Zero; }
        }
        bool SetLimits(uint flags)
        {
            var info = new ExtendedLimits();
            info.Basic.LimitFlags = flags;
            return SetInformationJobObject(handle, JobObjectExtendedLimitInformation, ref info, Marshal.SizeOf(typeof(ExtendedLimits)));
        }
        // Called right after Start and BEFORE the request goes to stdin: the worker blocks on
        // stdin first, so it cannot have spawned anything that would escape the job yet.
        public void Assign(Process process)
        {
            rootPid = process.Id;
            try { Assigned = handle != IntPtr.Zero && AssignProcessToJobObject(handle, process.Handle); }
            catch { Assigned = false; }
        }
        // Kill every process of the tree. Without a job (creation or assignment refused),
        // fall back to taskkill /T, which still reaches every descendant whose parent lives.
        public void Kill()
        {
            if (Assigned && TerminateJobObject(handle, 1)) return;
            if (rootPid == 0) return;
            try
            {
                var psi = new ProcessStartInfo(System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "taskkill.exe"),
                    "/PID " + rootPid + " /T /F") { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
                using (var kill = Process.Start(psi))
                {
                    kill.StandardOutput.ReadToEndAsync(); kill.StandardError.ReadToEndAsync();
                    kill.WaitForExit(15000);
                }
            }
            catch { }
        }
        // Normal completion: whatever the worker left behind keeps today's behaviour
        // (nothing is killed when the window closes later).
        public void Release()
        {
            if (handle != IntPtr.Zero) SetLimits(LimitBreakawayOk);
        }
        public void Dispose()
        {
            if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; }
        }
    }
}
