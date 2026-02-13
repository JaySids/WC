import React, { useState } from 'react';
import Editor from '@monaco-editor/react';
import { FileMap } from '../types';

interface PreviewPaneProps {
  isVisible: boolean;
  previewUrl: string;
  iframeRef: React.RefObject<HTMLIFrameElement | null>;
  files: FileMap;
  activeFile: string;
  setActiveFile: (path: string) => void;
  onFileEdit: (filepath: string, content: string) => void;
  onFilesTabClick: () => void;
  isCloning: boolean;
}

function fileExt(path: string): string {
  const parts = path.split('.');
  return parts.length > 1 ? parts.pop()! : '';
}

function extColor(ext: string): string {
  switch (ext) {
    case 'jsx': case 'tsx': return 'bg-blue-400';
    case 'html': return 'bg-orange-400';
    case 'css': return 'bg-yellow-400';
    case 'js': case 'ts': return 'bg-yellow-300';
    case 'json': return 'bg-green-400';
    default: return 'bg-gray-500';
  }
}

function editorLang(path: string): string {
  const ext = fileExt(path);
  switch (ext) {
    case 'jsx': case 'tsx': return 'javascript';
    case 'ts': return 'typescript';
    case 'html': return 'html';
    case 'css': return 'css';
    case 'json': return 'json';
    default: return 'javascript';
  }
}

type TreeNode = { type: 'folder' | 'file'; path: string; name: string; depth: number };

function buildTree(paths: string[]): TreeNode[] {
  const nodes: TreeNode[] = [];
  const addedFolders = new Set<string>();
  const sorted = [...paths].sort();
  for (const p of sorted) {
    const parts = p.split('/');
    for (let i = 1; i < parts.length; i++) {
      const folder = parts.slice(0, i).join('/');
      if (!addedFolders.has(folder)) {
        addedFolders.add(folder);
        nodes.push({ type: 'folder', path: folder, name: parts[i - 1], depth: i - 1 });
      }
    }
    nodes.push({ type: 'file', path: p, name: parts[parts.length - 1], depth: parts.length - 1 });
  }
  return nodes;
}

const PreviewPane: React.FC<PreviewPaneProps> = ({
  isVisible, previewUrl, iframeRef, files, activeFile, setActiveFile, onFileEdit, onFilesTabClick, isCloning,
}) => {
  const [tab, setTab] = useState<'preview' | 'files'>('preview');

  const fileNames = Object.keys(files);
  const treeNodes = buildTree(fileNames);

  return (
    <section className={`flex-1 flex flex-col min-w-0 bg-[#09090b] relative border-r border-border-dark ${!isVisible ? 'hidden' : ''}`}>
      {/* Tab bar + URL bar */}
      <div className="h-10 bg-[#0c0c0e] border-b border-border-dark flex items-center px-2 gap-0 shrink-0">
        <button
          onClick={() => setTab('preview')}
          className={`px-3 py-2 text-[10px] font-mono uppercase tracking-wider border-b-2 transition-colors ${
            tab === 'preview' ? 'border-blue-500 text-blue-400' : 'border-transparent text-gray-500 hover:text-white'
          }`}
        >
          Preview
        </button>
        <button
          onClick={() => { setTab('files'); onFilesTabClick(); }}
          className={`px-3 py-2 text-[10px] font-mono uppercase tracking-wider border-b-2 transition-colors ${
            tab === 'files' ? 'border-blue-500 text-blue-400' : 'border-transparent text-gray-500 hover:text-white'
          }`}
        >
          Files{fileNames.length > 0 ? ` (${fileNames.length})` : ''}
        </button>

        {/* URL bar */}
        {tab === 'preview' && previewUrl && (
          <div className="flex-1 flex items-center gap-2 ml-3">
            <div className="flex-1 bg-black/20 rounded-sm h-6 flex items-center px-2 text-[10px] text-gray-500 font-mono border border-white/5 truncate">
              {previewUrl}
            </div>
            <a
              href={previewUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[10px] text-blue-400 hover:text-blue-300 font-mono transition-colors"
            >
              Open
            </a>
            <button
              className="text-[10px] text-gray-400 hover:text-white font-mono transition-colors"
              onClick={() => {
                if (iframeRef.current && previewUrl) {
                  try {
                    const u = new URL(previewUrl);
                    u.searchParams.set('_t', Date.now().toString());
                    iframeRef.current.src = u.toString();
                  } catch {
                    iframeRef.current.src = previewUrl;
                  }
                }
              }}
            >
              Reload
            </button>
          </div>
        )}
      </div>

      {/* Tab content */}
      {tab === 'preview' ? (
        previewUrl ? (
          <iframe
            ref={iframeRef}
            src={previewUrl}
            className="flex-1 w-full bg-white"
            title="Clone preview"
            allow="cross-origin-isolated"
            onError={() => console.warn('iframe error:', previewUrl)}
          />
        ) : (
          <div className="flex-1 flex flex-col items-center justify-center text-gray-700 space-y-3">
            <span className="material-symbols-outlined text-5xl opacity-20">web</span>
            <span className="text-xs font-mono text-gray-600">
              {isCloning ? 'Preview will appear once deployed...' : 'Clone a website to see the preview'}
            </span>
          </div>
        )
      ) : (
        /* Files tab */
        <div className="flex-1 flex overflow-hidden">
          {/* File tree sidebar */}
          <div className="w-52 shrink-0 border-r border-border-dark overflow-y-auto custom-scrollbar py-2">
            <div className="px-3 pb-2 text-[9px] font-mono text-gray-500 uppercase tracking-wider">
              Files ({fileNames.length})
            </div>
            {treeNodes.map(node => {
              if (node.type === 'folder') {
                return (
                  <div
                    key={`f-${node.path}`}
                    className="w-full text-left px-3 py-0.5 flex items-center gap-1.5 text-gray-500"
                    style={{ paddingLeft: `${10 + node.depth * 10}px` }}
                  >
                    <span className="material-symbols-outlined text-[12px]">folder</span>
                    <span className="font-mono text-[10px] font-medium">{node.name}</span>
                  </div>
                );
              }
              const ext = fileExt(node.path);
              return (
                <button
                  key={node.path}
                  onClick={() => setActiveFile(node.path)}
                  className={`w-full text-left px-3 py-0.5 flex items-center gap-2 hover:bg-white/[0.03] transition-colors ${
                    activeFile === node.path ? 'bg-white/[0.05] text-white' : 'text-gray-400'
                  }`}
                  style={{ paddingLeft: `${10 + node.depth * 10}px` }}
                >
                  <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${extColor(ext)}`} />
                  <span className="font-mono text-[10px] truncate">{node.name}</span>
                </button>
              );
            })}
            {fileNames.length === 0 && (
              <p className="px-3 text-[10px] text-gray-700 font-mono">No files yet</p>
            )}
          </div>

          {/* Editor */}
          <div className="flex-1">
            {activeFile && files[activeFile] !== undefined ? (
              <Editor
                height="100%"
                language={editorLang(activeFile)}
                value={files[activeFile]}
                onChange={(value) => onFileEdit(activeFile, value || '')}
                theme="vs-dark"
                options={{
                  minimap: { enabled: false },
                  fontSize: 11,
                  lineNumbers: 'on',
                  scrollBeyondLastLine: false,
                  wordWrap: 'on',
                  tabSize: 2,
                }}
              />
            ) : (
              <div className="flex-1 h-full flex items-center justify-center">
                <p className="text-[10px] text-gray-600 font-mono">
                  {fileNames.length > 0 ? 'Select a file to edit' : 'Files will appear here during cloning'}
                </p>
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
};

export default PreviewPane;
