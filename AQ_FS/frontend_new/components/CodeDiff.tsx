import React from 'react';
import { CodeDiffBlock } from '../types';

interface CodeDiffProps {
  data: CodeDiffBlock;
}

const CodeDiff: React.FC<CodeDiffProps> = ({ data }) => {
  return (
    <div className="bg-[#050505] rounded border border-border-dark overflow-hidden font-mono text-xs my-3">
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-border-dark bg-[#0c0c0e]">
        <span className="text-gray-500 text-[10px]">{data.filename}</span>
        <span className="text-[10px] text-gray-400 cursor-pointer hover:text-white transition-colors">copy</span>
      </div>
      <div className="p-3 overflow-x-auto">
        <code className="language-css block">
          {data.lines.map((line, index) => {
            const isAddition = line.type === 'addition';
            // Simplified syntax highlighting for demo purposes
            // In a real app, a parser would be used
            const lineContent = line.content;
            
            return (
              <div key={index} className="flex">
                <span className="text-gray-600 w-6 select-none text-right pr-2">{line.lineNumber}</span>
                <span 
                  className={`w-full block pl-2 border-l-2 ${
                    isAddition 
                      ? 'text-emerald-500 bg-emerald-950/20 border-emerald-700' 
                      : 'text-gray-400 border-transparent'
                  }`}
                >
                  {/* Basic regex replacers to simulate syntax highlighting */}
                  {lineContent.split(/([{},:;])/).map((part, i) => {
                    if (part.match(/[{}]/)) return <span key={i} className="token-punctuation">{part}</span>;
                    if (part.startsWith('.')) return <span key={i} className="token-selector">{part}</span>;
                    if (part.includes(':')) return <span key={i} className="token-property">{part}</span>;
                    // Check if it's a value (after colon usually, but simplified here)
                    if (part.trim().length > 0 && !part.match(/[{}:;]/) && !part.startsWith('.')) {
                        // Detect if likely a property or value
                        return <span key={i} className={isAddition ? 'text-emerald-400' : 'text-gray-300'}>{part}</span>;
                    }
                    return part;
                  })}
                </span>
              </div>
            );
          })}
        </code>
      </div>
    </div>
  );
};

export default CodeDiff;