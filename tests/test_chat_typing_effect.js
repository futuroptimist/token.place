const assert = require('assert');

function runTest(name, fn) {
    try {
        fn();
        console.log(`✓ ${name}`);
    } catch (error) {
        console.error(`✗ ${name}`);
        console.error(error);
        process.exitCode = 1;
    }
}

const { createTypingAnimator } = require('../static/chat_typing.js');

runTest('typing animator progressively reveals text', () => {
    const updates = [];
    const scheduled = [];

    const animator = createTypingAnimator({
        fullText: 'token.place',
        chunkSize: 3,
        interval: 0,
        schedule: (fn) => {
            scheduled.push(fn);
            return scheduled.length - 1;
        },
        cancelScheduled: () => {},
        onUpdate: (value) => updates.push(value),
        onComplete: () => updates.push('<<done>>'),
    });

    animator.start();
    while (scheduled.length > 0) {
        const next = scheduled.shift();
        next();
    }

    assert.deepStrictEqual(
        updates,
        ['', 'tok', 'token.', 'token.pla', 'token.place', '<<done>>']
    );
});

runTest('cancel stops scheduled updates', () => {
    const updates = [];
    const scheduled = [];

    const animator = createTypingAnimator({
        fullText: 'token.place',
        chunkSize: 2,
        interval: 0,
        schedule: (fn) => {
            scheduled.push(fn);
            return scheduled.length - 1;
        },
        cancelScheduled: () => {},
        onUpdate: (value) => updates.push(value),
        onComplete: () => updates.push('<<done>>'),
    });

    animator.start();
    const first = scheduled.shift();
    first();

    animator.cancel();

    while (scheduled.length > 0) {
        const next = scheduled.shift();
        next();
    }

    assert.deepStrictEqual(updates, ['', 'to']);
});
