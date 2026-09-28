import { useCallback, useEffect, useRef, useState } from "react";

/** The slice of the Web Speech API we use (Chrome/Edge expose it prefixed). */
interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  stop(): void;
  onresult: ((event: SpeechResultEvent) => void) | null;
  onend: (() => void) | null;
  onerror: (() => void) | null;
}

interface SpeechResultEvent {
  resultIndex: number;
  results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }>;
}

type RecognitionCtor = new () => SpeechRecognitionLike;

function recognitionCtor(): RecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: RecognitionCtor;
    webkitSpeechRecognition?: RecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

/**
 * Dictation for the composer. Feature-detected: a browser without the Web
 * Speech API reports `supported: false`, and the caller hides the control
 * rather than offering a button that cannot work.
 */
export function useVoiceInput(onText: (text: string) => void) {
  const [listening, setListening] = useState(false);
  const recognition = useRef<SpeechRecognitionLike | null>(null);
  // Dictation keeps running while the composer re-renders on every transcript,
  // so the result handler reads the callback from here rather than from a
  // closure captured when recording started.
  const latest = useRef(onText);
  useEffect(() => {
    latest.current = onText;
  }, [onText]);

  const stop = useCallback(() => {
    recognition.current?.stop();
    recognition.current = null;
    setListening(false);
  }, []);

  const start = useCallback(() => {
    const Ctor = recognitionCtor();
    if (!Ctor) return;
    const rec = new Ctor();
    rec.lang = "zh-CN";
    rec.continuous = true;
    rec.interimResults = false;
    rec.onresult = (event) => {
      let text = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        const alternative = result?.[0];
        if (result?.isFinal && alternative) text += alternative.transcript;
      }
      if (text) latest.current(text);
    };
    rec.onend = () => setListening(false);
    rec.onerror = () => setListening(false);
    recognition.current = rec;
    rec.start();
    setListening(true);
  }, []);

  useEffect(() => () => recognition.current?.stop(), []);

  const toggle = useCallback(() => {
    if (listening) stop();
    else start();
  }, [listening, start, stop]);

  return { supported: recognitionCtor() !== null, listening, toggle };
}
