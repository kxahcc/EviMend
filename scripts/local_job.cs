using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace DfxLocal {
    // Only a job that has been successfully configured may start a process.
    public sealed class BoundedJob : IDisposable {
        [StructLayout(LayoutKind.Sequential)]
        private struct BasicLimits {
            public long ProcessTime;
            public long JobTime;
            public uint Flags;
            public UIntPtr MinWorkingSet;
            public UIntPtr MaxWorkingSet;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters {
            public ulong ReadOperations, WriteOperations, OtherOperations;
            public ulong ReadBytes, WriteBytes, OtherBytes;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedLimits {
            public BasicLimits Basic;
            public IoCounters Io;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemory;
            public UIntPtr PeakJobMemory;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct CpuLimits { public uint Flags; public uint CpuRate; }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct StartupInfo {
            public uint Size;
            public IntPtr Reserved, Desktop, Title;
            public uint X, Y, XSize, YSize, XChars, YChars, Fill, Flags;
            public ushort ShowWindow, Reserved2;
            public IntPtr ReservedBytes, StdInput, StdOutput, StdError;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct ProcessInfo {
            public IntPtr Process, Thread;
            public uint ProcessId, ThreadId;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct MemoryStatus {
            public uint Size, Load;
            public ulong TotalPhysical, AvailablePhysical, TotalPageFile, AvailablePageFile;
            public ulong TotalVirtual, AvailableVirtual, AvailableExtendedVirtual;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes {
            public uint Size;
            public IntPtr SecurityDescriptor;
            [MarshalAs(UnmanagedType.Bool)] public bool InheritHandle;
        }
        [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits value, uint length);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref CpuLimits value, uint length);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool QueryInformationJobObject(IntPtr job, int infoClass, out ExtendedLimits value, uint length, IntPtr retLength);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool QueryInformationJobObject(IntPtr job, int infoClass, out CpuLimits value, uint length, IntPtr retLength);
        [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
        private static extern bool CreateProcess(string application, StringBuilder commandLine, IntPtr processAttributes,
            IntPtr threadAttributes, bool inheritHandles, uint flags, IntPtr environment, string currentDirectory,
            ref StartupInfo startup, out ProcessInfo info);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern uint ResumeThread(IntPtr thread);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool TerminateProcess(IntPtr process, uint exitCode);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool TerminateJobObject(IntPtr job, uint exitCode);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool GlobalMemoryStatusEx(ref MemoryStatus status);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool CreatePipe(out IntPtr read, out IntPtr write, ref SecurityAttributes attributes, uint size);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool ReadFile(IntPtr handle, byte[] buffer, uint bytesToRead, out uint bytesRead, IntPtr overlapped);
        [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
        private static extern IntPtr CreateFile(string name, uint access, uint share, ref SecurityAttributes attributes,
            uint disposition, uint flags, IntPtr template);

        private IntPtr job, process, readPipe, initialThread;
        private Thread outputThread;
        private volatile bool logExceeded;
        private Exception outputError;
        private bool disposed;
        public uint ProcessId { get; private set; }
        public ulong MemoryLimitBytes { get; private set; }
        public bool LogExceeded => logExceeded;
        public string OutputError => outputError?.Message;

        private static Exception Error(string action) => new Win32Exception(Marshal.GetLastWin32Error(), action);
        public static ulong[] PhysicalMemory() {
            MemoryStatus m = new MemoryStatus { Size = (uint)Marshal.SizeOf<MemoryStatus>() };
            if (!GlobalMemoryStatusEx(ref m)) throw Error("GlobalMemoryStatusEx failed");
            return new ulong[] { m.TotalPhysical, m.AvailablePhysical };
        }

        public BoundedJob(int cpuPercent, ulong memoryBytes, uint maxProcesses)
            : this(cpuPercent, memoryBytes, maxProcesses, null) { }

        public BoundedJob(int cpuPercent, ulong memoryBytes, uint maxProcesses, string jobName) {
            if (cpuPercent < 1 || cpuPercent > 100 || memoryBytes < 16 * 1024 * 1024 || maxProcesses < 1)
                throw new ArgumentOutOfRangeException("Invalid Job limits");
            job = CreateJobObject(IntPtr.Zero, jobName);
            if (job == IntPtr.Zero) throw Error("CreateJobObject failed");
            try {
                if (jobName != null && Marshal.GetLastWin32Error() == 183)
                    throw new InvalidOperationException("Named managed Job already exists");
                ExtendedLimits limits = new ExtendedLimits();
                // JOB_MEMORY | ACTIVE_PROCESS | KILL_ON_JOB_CLOSE; no breakaway flags.
                limits.Basic.Flags = 0x2000u | 0x8u | 0x200u;
                limits.Basic.ActiveProcessLimit = maxProcesses;
                limits.JobMemoryLimit = (UIntPtr)memoryBytes;
                uint extSize = (uint)Marshal.SizeOf<ExtendedLimits>();
                if (!SetInformationJobObject(job, 9, ref limits, extSize)) throw Error("Set extended Job limits failed");
                CpuLimits cpu = new CpuLimits { Flags = 0x1u | 0x4u, CpuRate = (uint)(cpuPercent * 100) };
                uint cpuSize = (uint)Marshal.SizeOf<CpuLimits>();
                if (!SetInformationJobObject(job, 15, ref cpu, cpuSize)) throw Error("Set Job CPU hard cap failed");
                ExtendedLimits actual;
                CpuLimits actualCpu;
                if (!QueryInformationJobObject(job, 9, out actual, extSize, IntPtr.Zero) ||
                    !QueryInformationJobObject(job, 15, out actualCpu, cpuSize, IntPtr.Zero) ||
                    actual.Basic.Flags != limits.Basic.Flags ||
                    actual.Basic.ActiveProcessLimit != maxProcesses ||
                    actual.JobMemoryLimit.ToUInt64() != memoryBytes ||
                    actualCpu.Flags != cpu.Flags || actualCpu.CpuRate != cpu.CpuRate)
                    throw new InvalidOperationException("Job limit query did not match requested policy");
                MemoryLimitBytes = memoryBytes;
            } catch { CloseHandle(job); job = IntPtr.Zero; throw; }
        }

        public void Start(string executable, string script, string scenario, string logPath, string workDirectory,
            int seconds, long maxLogBytes) {
            StartSuspended(executable, script, scenario, logPath, workDirectory, seconds, maxLogBytes);
            try { Resume(); }
            catch { Terminate(); throw; }
        }

        // The host must publish the complete lease before explicitly resuming.
        public void StartSuspended(string executable, string script, string scenario, string logPath,
            string workDirectory, int seconds, long maxLogBytes) {
            if (disposed || ProcessId != 0) throw new InvalidOperationException("Job already used");
            if (maxLogBytes < 1024) throw new ArgumentOutOfRangeException(nameof(maxLogBytes));
            if (scenario != "idle" && scenario != "child" && scenario != "noisy" &&
                scenario != "memory" && scenario != "bootstrap" && scenario != "test" &&
                scenario != "docs" && scenario != "ruff" && scenario != "pyright" && scenario != "offline" &&
                scenario != "aggregate" && scenario != "export" && scenario != "competition")
                throw new ArgumentException("Unsupported synthetic scenario");
            // Fixed executable and fixed script, with bounded data-only arguments.
            string command = "\"" + executable + "\" -NoProfile -NonInteractive -File \"" + script +
                "\" -Scenario " + scenario + " -Seconds " + seconds;
            SecurityAttributes attributes = new SecurityAttributes {
                Size = (uint)Marshal.SizeOf<SecurityAttributes>(), InheritHandle = true
            };
            IntPtr writePipe = IntPtr.Zero, nullInput = IntPtr.Zero;
            if (!CreatePipe(out readPipe, out writePipe, ref attributes, 0)) throw Error("CreatePipe failed");
            try {
                if (!SetHandleInformation(readPipe, 1, 0)) throw Error("SetHandleInformation failed");
                nullInput = CreateFile("NUL", 0x80000000u, 3, ref attributes, 3, 0, IntPtr.Zero);
                if (nullInput == new IntPtr(-1)) throw Error("CreateFile NUL failed");
                StartupInfo startup = new StartupInfo { Size = (uint)Marshal.SizeOf<StartupInfo>(),
                    Flags = 0x100u, StdInput = nullInput, StdOutput = writePipe, StdError = writePipe };
            ProcessInfo info;
            if (!CreateProcess(executable, new StringBuilder(command), IntPtr.Zero, IntPtr.Zero, true,
                    0x4u | 0x08000000u, IntPtr.Zero, workDirectory, ref startup, out info))
                throw Error("Create suspended child failed");
            try {
                if (!AssignProcessToJobObject(job, info.Process)) throw Error("AssignProcessToJobObject failed");
                // Output consumer is live before the child can produce bytes.
                outputThread = new Thread(() => DrainOutput(logPath, maxLogBytes)) { IsBackground = true };
                outputThread.Start();
                process = info.Process;
                ProcessId = info.ProcessId;
                initialThread = info.Thread;
            } catch {
                TerminateProcess(info.Process, 1);
                CloseHandle(info.Process);
                throw;
            } finally { if (initialThread != info.Thread) CloseHandle(info.Thread); }
            } finally {
                if (writePipe != IntPtr.Zero) CloseHandle(writePipe);
                if (nullInput != IntPtr.Zero && nullInput != new IntPtr(-1)) CloseHandle(nullInput);
            }
        }

        public void Resume() {
            if (disposed || process == IntPtr.Zero || initialThread == IntPtr.Zero)
                throw new InvalidOperationException("Job has no suspended worker to resume");
            if (outputError != null || logExceeded)
                throw new InvalidOperationException("Output capture failed before worker resume");
            uint count = ResumeThread(initialThread);
            if (count == 0xffffffffu) throw Error("ResumeThread failed");
            if (count != 1u) {
                Terminate();
                throw new InvalidOperationException("Worker suspend count changed before resume");
            }
            CloseHandle(initialThread);
            initialThread = IntPtr.Zero;
        }

        private void DrainOutput(string logPath, long maxLogBytes) {
            try {
                using (var stream = new FileStream(logPath, FileMode.CreateNew, FileAccess.Write, FileShare.Read)) {
                    byte[] buffer = new byte[8192];
                    while (true) {
                        uint count;
                        if (!ReadFile(readPipe, buffer, (uint)buffer.Length, out count, IntPtr.Zero)) {
                            int error = Marshal.GetLastWin32Error();
                            if (error == 109) break;
                            throw new Win32Exception(error, "ReadFile output pipe failed");
                        }
                        if (count == 0) continue;
                        long left = maxLogBytes - stream.Length;
                        if (left > 0) { stream.Write(buffer, 0, (int)Math.Min(left, (long)count)); stream.Flush(); }
                        if ((long)count > left) {
                            logExceeded = true;
                            if (job != IntPtr.Zero) TerminateJobObject(job, 1);
                        }
                    }
                }
            } catch (Exception error) {
                outputError = error;
                if (job != IntPtr.Zero) TerminateJobObject(job, 1);
            }
        }

        public bool HasExited() => process != IntPtr.Zero && WaitForSingleObject(process, 0) == 0;
        public bool WaitForExit(uint milliseconds) => process != IntPtr.Zero && WaitForSingleObject(process, milliseconds) == 0;
        public uint ExitCode() {
            if (!HasExited()) throw new InvalidOperationException("Process is still running");
            uint code;
            if (!GetExitCodeProcess(process, out code)) throw Error("GetExitCodeProcess failed");
            return code;
        }
        public void Terminate() {
            if (job != IntPtr.Zero && !TerminateJobObject(job, 1)) throw Error("TerminateJobObject failed");
        }
        public void Dispose() {
            if (disposed) return;
            disposed = true;
            if (initialThread != IntPtr.Zero) { CloseHandle(initialThread); initialThread = IntPtr.Zero; }
            if (process != IntPtr.Zero) { CloseHandle(process); process = IntPtr.Zero; }
            if (job != IntPtr.Zero) { CloseHandle(job); job = IntPtr.Zero; }
            if (outputThread != null && !outputThread.Join(5000))
                outputError = new TimeoutException("Output pipe did not drain after Job closed");
            if (readPipe != IntPtr.Zero) { CloseHandle(readPipe); readPipe = IntPtr.Zero; }
            GC.SuppressFinalize(this);
        }
        ~BoundedJob() { Dispose(); }
    }
}
