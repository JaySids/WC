import React from 'react';

export enum Sender {
  SYSTEM = 'SYSTEM',
  BOT = 'CLONE_BOT',
  USER = 'USER'
}

export interface CodeDiffLine {
  lineNumber: number;
  content: string;
  type: 'neutral' | 'addition' | 'deletion';
}

export interface CodeDiffBlock {
  filename: string;
  language: string;
  lines: CodeDiffLine[];
}

export interface Message {
  id: string;
  timestamp: string;
  sender: Sender;
  text?: string;
  content?: React.ReactNode;
  codeDiff?: CodeDiffBlock;
  metadata?: {
    isUserAdmin?: boolean;
    domUpdateStatus?: string;
    previewUrl?: string;
    files?: string[];
    errors?: string[];
    issues?: string[];
    phase?: string;
    eventType?: string;
  };
}

export interface ChatSession {
  id: string;
  title: string;
  lastModified: number;
  messages: Message[];
  cloneId?: string;
  previewUrl?: string;
  url?: string;
  outputFormat?: 'html' | 'react';
  status?: string;
}

export interface CloneRecord {
  id: string;
  url: string;
  status: string;
  preview_url?: string;
  sandbox_id?: string;
  is_active?: boolean;
  output_format?: string;
  created_at?: string;
  metadata?: any;
}

export interface AgentEvent {
  type: string;
  [key: string]: any;
}

export interface FileMap {
  [path: string]: string;
}

export interface PipelineStep {
  step: string;
  message: string;
  status: 'active' | 'completed';
}
