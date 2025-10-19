(function (root, factory) {
    if (typeof module === 'object' && module.exports) {
        module.exports = factory();
    } else {
        root.ChatTypingEffect = factory();
    }
}(typeof self !== 'undefined' ? self : this, function () {
    function toStringValue(value) {
        if (value === null || value === undefined) {
            return '';
        }
        return String(value);
    }

    function createTypingAnimator(options) {
        if (!options || typeof options.onUpdate !== 'function') {
            throw new TypeError('createTypingAnimator requires an onUpdate callback');
        }

        const fullText = toStringValue(options.fullText ?? options.text ?? '');
        const totalLength = fullText.length;
        const chunkSizeCandidate = Number.isFinite(options.chunkSize) && options.chunkSize > 0
            ? Math.floor(options.chunkSize)
            : Math.max(1, Math.ceil(totalLength / 32));
        const chunkSize = Math.max(1, chunkSizeCandidate);
        const interval = Number.isFinite(options.interval) && options.interval >= 0
            ? options.interval
            : 35;
        const scheduler = typeof options.schedule === 'function'
            ? options.schedule
            : (fn, delay) => setTimeout(fn, delay);
        const canceller = typeof options.cancelScheduled === 'function'
            ? options.cancelScheduled
            : (id) => clearTimeout(id);
        const onComplete = typeof options.onComplete === 'function'
            ? options.onComplete
            : null;

        let index = 0;
        let cancelled = false;
        let timerId = null;
        let running = false;

        const emitStep = () => {
            if (cancelled) {
                return;
            }
            index = Math.min(totalLength, index + chunkSize);
            options.onUpdate(fullText.slice(0, index));
            if (index >= totalLength) {
                cancelled = true;
                running = false;
                if (onComplete) {
                    onComplete(fullText);
                }
                return;
            }
            timerId = scheduler(emitStep, interval);
        };

        return {
            start() {
                if (running || cancelled) {
                    return;
                }
                running = true;
                options.onUpdate('');
                if (totalLength === 0) {
                    cancelled = true;
                    running = false;
                    if (onComplete) {
                        onComplete('');
                    }
                    return;
                }
                timerId = scheduler(emitStep, interval);
            },
            cancel() {
                if (cancelled) {
                    return;
                }
                cancelled = true;
                running = false;
                if (timerId !== null) {
                    try {
                        canceller(timerId);
                    } catch (error) {
                        if (typeof console !== 'undefined' && console.warn) {
                            console.warn('Failed to cancel typing animator timer:', error);
                        }
                    }
                }
            },
            isRunning() {
                return running && !cancelled;
            },
        };
    }

    return { createTypingAnimator };
}));
