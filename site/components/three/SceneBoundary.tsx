'use client';

import { Component, type ReactNode } from 'react';

type Props = { children: ReactNode; fallback: ReactNode };
type State = { failed: boolean };

export default class SceneBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch() {
    // The static fallback is the intended outcome; no console noise from the page.
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}