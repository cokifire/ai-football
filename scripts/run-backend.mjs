// 跨平台启动后端：自动探测项目根目录 venv 中的 Python 解释器
// 支持 Windows (venv/Scripts/python.exe) 与 Linux/macOS (venv/bin/python)
// 注意：从 Windows 拷贝过来的 venv/Scripts/python.exe 在 Linux 上不可执行，
//       因此必须按当前平台筛选候选路径，不能直接 existsSync 后就使用。
import { spawn } from 'node:child_process';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(scriptDir, '..'); // 项目根目录 (ai-football/)
const backendDir = path.join(root, 'backend');
const isWin = process.platform === 'win32';

function exists(p) {
  try { fs.accessSync(p, fs.constants.F_OK); return true; } catch { return false; }
}
function executable(p) {
  try { fs.accessSync(p, fs.constants.X_OK); return true; } catch { return false; }
}

function findPython() {
  // 仅按当前平台筛选 venv 内的候选，避免误用其它平台的解释器
  const venvCandidates = isWin
    ? [path.join(root, 'venv', 'Scripts', 'python.exe')]
    : [
        path.join(root, 'venv', 'bin', 'python'),
        path.join(root, 'venv', 'bin', 'python3'),
      ];

  for (const c of venvCandidates) {
    if (exists(c) && (isWin || executable(c))) return c;
  }

  // 回退：依赖当前已激活的虚拟环境或系统 python/python3
  return isWin ? 'python' : 'python3';
}

const python = findPython();
console.log(`[backend] 使用 Python: ${python}`);
console.log(`[backend] 工作目录: ${backendDir}`);

const child = spawn(
  python,
  ['-m', 'uvicorn', 'main:app', '--host', '0.0.0.0', '--port', '8000'],
  { cwd: backendDir, stdio: 'inherit', shell: false }
);

child.on('exit', (code) => process.exit(code ?? 1));
process.on('SIGINT', () => child.kill('SIGINT'));
process.on('SIGTERM', () => child.kill('SIGTERM'));
