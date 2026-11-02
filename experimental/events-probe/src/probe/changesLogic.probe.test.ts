import { describe, expect, test } from 'bun:test';

import { compareContent, detectChanges, EVENT, initialize, type ProbeEvent, type ProbeNode, type ProbeSdk } from './changesLogic';

/** In-memory Drive: nodes by uid, plus an event log. */
class FakeDrive implements ProbeSdk {
    nodes = new Map<string, ProbeNode>();
    events: ProbeEvent[] = [];
    seq = 0;
    failHierarchy = false;

    constructor() {
        this.add({ uid: 'root', name: 'root', type: 'folder' });
        this.add({ uid: 'W', parentUid: 'root', name: 'From_Proton', type: 'folder' });
        this.add({ uid: 'other', parentUid: 'root', name: 'Other', type: 'folder' });
    }

    add(n: Partial<ProbeNode> & { uid: string; name: string; type: string }): void {
        this.nodes.set(n.uid, { trashed: false, ...n } as ProbeNode);
    }

    hierarchyCalls = 0;

    /** Like Proton: the event carries the node's parent after the change, in clear. */
    emit(type: string, nodeUid?: string): void {
        this.seq++;
        const parentNodeUid = nodeUid ? this.nodes.get(nodeUid)?.parentUid : undefined;
        this.events.push({ type, eventId: `e${this.seq}`, nodeUid, parentNodeUid });
    }

    async getHierarchy(uid: string): Promise<ProbeNode[] | null> {
        this.hierarchyCalls++;
        if (this.failHierarchy) {
            throw new Error('network down');
        }
        const chain: ProbeNode[] = [];
        let cur = this.nodes.get(uid);
        if (!cur) {
            return null;
        }
        while (cur) {
            chain.unshift(cur);
            cur = cur.parentUid ? this.nodes.get(cur.parentUid) : undefined;
        }
        return chain;
    }

    async listChildren(folderUid: string): Promise<ProbeNode[]> {
        return [...this.nodes.values()].filter((n) => n.parentUid === folderUid);
    }

    async *iterateEvents(_scope: string, last?: string): AsyncIterable<ProbeEvent> {
        if (!last) {
            yield { type: EVENT.FastForward, eventId: `e${this.seq}` };
            return;
        }
        const start = this.events.findIndex((e) => e.eventId === last) + 1;
        for (const e of this.events.slice(start)) {
            yield e;
        }
    }
}

const FOLDER = { uid: 'W', path: '/my-files/From_Proton', scopeId: 'vol' };

async function started(drive: FakeDrive) {
    drive.emit(EVENT.NodeUpdated, 'zzz'); // something before the start: must not be reported
    const init = await initialize(drive, FOLDER);
    return init.newState!;
}

describe('probe change detection', () => {
    test('first run sets the bookmark and lists the folder, reports nothing', async () => {
        const d = new FakeDrive();
        d.add({ uid: 'a', parentUid: 'W', name: 'a.txt', type: 'file', size: 1 });
        d.add({ uid: 's', parentUid: 'W', name: 'sub', type: 'folder' });
        d.add({ uid: 'b', parentUid: 's', name: 'b.txt', type: 'file', size: 2 });
        const r = await initialize(d, FOLDER);
        expect(r.initialized).toBe(true);
        expect(r.changes).toEqual([]);
        expect(Object.values(r.newState!.known).map((k) => k.path).sort()).toEqual(['a.txt', 'sub', 'sub/b.txt']);
    });

    test('created, modified, renamed, trashed, deleted, moved out', async () => {
        const d = new FakeDrive();
        d.add({ uid: 'a', parentUid: 'W', name: 'a.txt', type: 'file', size: 1, modificationTime: 't1' });
        d.add({ uid: 'b', parentUid: 'W', name: 'b.txt', type: 'file', size: 1 });
        d.add({ uid: 'c', parentUid: 'W', name: 'c.txt', type: 'file', size: 1 });
        d.add({ uid: 'd', parentUid: 'W', name: 'd.txt', type: 'file', size: 1 });
        d.add({ uid: 'e', parentUid: 'W', name: 'e.txt', type: 'file', size: 1 });
        const state = await started(d);

        d.add({ uid: 'n', parentUid: 'W', name: 'new.pdf', type: 'file', size: 5 });
        d.emit(EVENT.NodeCreated, 'n');
        d.nodes.get('a')!.size = 9;
        d.nodes.get('a')!.modificationTime = 't2';
        d.emit(EVENT.NodeUpdated, 'a');
        d.nodes.get('b')!.name = 'b-renamed.txt';
        d.emit(EVENT.NodeUpdated, 'b');
        d.nodes.get('c')!.trashed = true;
        d.emit(EVENT.NodeUpdated, 'c');
        d.nodes.delete('d');
        d.emit(EVENT.NodeDeleted, 'd');
        d.nodes.get('e')!.parentUid = 'other';
        d.emit(EVENT.NodeUpdated, 'e');

        const r = await detectChanges(d, state);
        const summary = r.changes.map((c) => `${c.action} ${c.from ? c.from + '->' : ''}${c.path}${c.reason ? ' (' + c.reason + ')' : ''}`);
        expect(summary).toEqual([
            'created new.pdf',
            'modified a.txt',
            'moved b.txt->b-renamed.txt',
            'removed c.txt (trashed)',
            'removed d.txt (deleted)',
            'removed e.txt (moved out of the folder)',
        ]);
        expect(r.eventsRead).toBe(6);
        expect(r.newState!.lastEventId).toBe(d.events.at(-1)!.eventId);
        // Running again from the new bookmark reports nothing.
        const again = await detectChanges(d, r.newState!);
        expect(again.changes).toEqual([]);
    });

    test('activity outside the folder is counted but not reported', async () => {
        const d = new FakeDrive();
        const state = await started(d);
        d.add({ uid: 'x', parentUid: 'other', name: 'x.txt', type: 'file' });
        d.emit(EVENT.NodeCreated, 'x');
        const r = await detectChanges(d, state);
        expect(r.eventsRead).toBe(1);
        expect(r.changes).toEqual([]);
    });

    test('several events on one node give one change', async () => {
        const d = new FakeDrive();
        const state = await started(d);
        d.add({ uid: 'n', parentUid: 'W', name: 'n.txt', type: 'file', size: 1 });
        d.emit(EVENT.NodeCreated, 'n');
        d.nodes.get('n')!.size = 2;
        d.emit(EVENT.NodeUpdated, 'n');
        const r = await detectChanges(d, state);
        expect(r.changes.map((c) => c.action)).toEqual(['created']);
        expect(r.changes[0]!.size).toBe(2);
    });

    test('a folder moved in brings its contents; moving a known folder moves its contents', async () => {
        const d = new FakeDrive();
        d.add({ uid: 'k', parentUid: 'W', name: 'kept', type: 'folder' });
        d.add({ uid: 'k1', parentUid: 'k', name: 'in.txt', type: 'file' });
        d.add({ uid: 'f', parentUid: 'other', name: 'photos', type: 'folder' });
        d.add({ uid: 'f1', parentUid: 'f', name: 'p1.jpg', type: 'file' });
        const state = await started(d);

        d.nodes.get('f')!.parentUid = 'W';
        d.emit(EVENT.NodeUpdated, 'f');
        d.nodes.get('k')!.name = 'renamed';
        d.emit(EVENT.NodeUpdated, 'k');
        const r = await detectChanges(d, state);
        expect(r.changes.map((c) => `${c.action} ${c.path}`)).toEqual([
            'created photos',
            'created photos/p1.jpg',
            'moved renamed',
        ]);
        expect(Object.values(r.newState!.known).map((k) => k.path).sort()).toEqual([
            'photos',
            'photos/p1.jpg',
            'renamed',
            'renamed/in.txt',
        ]);
    });

    test('a trashed folder is reported once, with its contents counted', async () => {
        const d = new FakeDrive();
        d.add({ uid: 's', parentUid: 'W', name: 'sub', type: 'folder' });
        d.add({ uid: 's1', parentUid: 's', name: '1.txt', type: 'file' });
        d.add({ uid: 's2', parentUid: 's', name: '2.txt', type: 'file' });
        const state = await started(d);
        d.nodes.get('s')!.trashed = true;
        d.emit(EVENT.NodeUpdated, 's');
        const r = await detectChanges(d, state);
        expect(r.changes.map((c) => `${c.action} ${c.path} (${c.reason})`)).toEqual([
            'removed sub (trashed, with the 2 item(s) it contained)',
        ]);
        expect(r.newState!.known).toEqual({});
    });

    test('a full refresh relists the folder and compares', async () => {
        const d = new FakeDrive();
        d.add({ uid: 'a', parentUid: 'W', name: 'a.txt', type: 'file', size: 1 });
        d.add({ uid: 'b', parentUid: 'W', name: 'b.txt', type: 'file', size: 1 });
        const state = await started(d);
        d.nodes.delete('a');
        d.nodes.get('b')!.size = 3;
        d.add({ uid: 'c', parentUid: 'W', name: 'c.txt', type: 'file', size: 1 });
        d.emit(EVENT.TreeRefresh);
        const r = await detectChanges(d, state);
        expect(r.changes.map((c) => `${c.action} ${c.path}`).sort()).toEqual([
            'created c.txt',
            'modified b.txt',
            'removed a.txt',
        ]);
        expect(r.notes[0]).toContain('full refresh');
    });

    test('the watched folder trashed: state reset', async () => {
        const d = new FakeDrive();
        const state = await started(d);
        d.nodes.get('W')!.trashed = true;
        d.emit(EVENT.NodeUpdated, 'W');
        const r = await detectChanges(d, state);
        expect(r.newState).toBeNull();
        expect(r.notes[0]).toContain('trashed or deleted');
    });

    test('volume no longer accessible: state reset', async () => {
        const d = new FakeDrive();
        const state = await started(d);
        d.emit(EVENT.TreeRemove);
        const r = await detectChanges(d, state);
        expect(r.newState).toBeNull();
    });

    test('a network failure fails the run instead of reporting removals', async () => {
        const d = new FakeDrive();
        d.add({ uid: 'a', parentUid: 'W', name: 'a.txt', type: 'file' });
        const state = await started(d);
        d.emit(EVENT.NodeUpdated, 'a');
        d.failHierarchy = true;
        await expect(detectChanges(d, state)).rejects.toThrow('network down');
    });

    test('the state passed in is not modified', async () => {
        const d = new FakeDrive();
        d.add({ uid: 's', parentUid: 'W', name: 'sub', type: 'folder' });
        d.add({ uid: 's1', parentUid: 's', name: '1.txt', type: 'file' });
        const state = await started(d);
        const copy = JSON.stringify(state);
        d.nodes.get('s')!.name = 'moved';
        d.emit(EVENT.NodeUpdated, 's');
        await detectChanges(d, state);
        expect(JSON.stringify(state)).toBe(copy);
    });
});

describe('content comparison (0.2)', () => {
    test('same SHA-1 and new revision: revised, not modified', async () => {
        const d = new FakeDrive();
        d.add({ uid: 'a', parentUid: 'W', name: 'a.pdf', type: 'file', size: 5, modificationTime: 't1', revisionUid: 'r1', sha1: 'h1' });
        const state = await started(d);
        d.nodes.get('a')!.revisionUid = 'r2';
        d.emit(EVENT.NodeUpdated, 'a');
        const r = await detectChanges(d, state);
        expect(r.changes.map((c) => `${c.action} ${c.path} (${c.reason})`)).toEqual(['revised a.pdf (new version, identical content)']);
        expect(r.newState!.known['a']!.revisionUid).toBe('r2');
    });

    test('different SHA-1 at equal size and date: modified', async () => {
        const d = new FakeDrive();
        d.add({ uid: 'a', parentUid: 'W', name: 'a.pdf', type: 'file', size: 5, modificationTime: 't1', revisionUid: 'r1', sha1: 'h1' });
        const state = await started(d);
        d.nodes.get('a')!.revisionUid = 'r2';
        d.nodes.get('a')!.sha1 = 'h2';
        d.emit(EVENT.NodeUpdated, 'a');
        const r = await detectChanges(d, state);
        expect(r.changes.map((c) => c.action)).toEqual(['modified']);
    });

    test('new revision without SHA-1 to prove it identical: modified', () => {
        expect(
            compareContent(
                { path: 'a', type: 'file', size: 1, modificationTime: 't', revisionUid: 'r1' },
                { path: 'a', type: 'file', size: 1, modificationTime: 't', revisionUid: 'r2' },
            ),
        ).toBe('changed');
    });

    test('a 0.1 state (no revision, no SHA-1) does not report spurious changes', async () => {
        const d = new FakeDrive();
        d.add({ uid: 'a', parentUid: 'W', name: 'a.pdf', type: 'file', size: 5, modificationTime: 't1', revisionUid: 'r1', sha1: 'h1' });
        const state = await started(d);
        state.known['a'] = { path: 'a.pdf', type: 'file', size: 5, modificationTime: 't1' };
        d.emit(EVENT.NodeUpdated, 'a');
        const r = await detectChanges(d, state);
        expect(r.changes).toEqual([]);
        expect(r.newState!.known['a']!.sha1).toBe('h1');
    });

    test('renamed with new content: one moved change that says so', async () => {
        const d = new FakeDrive();
        d.add({ uid: 'a', parentUid: 'W', name: 'a.pdf', type: 'file', size: 5, sha1: 'h1', revisionUid: 'r1' });
        const state = await started(d);
        d.nodes.get('a')!.name = 'b.pdf';
        d.nodes.get('a')!.sha1 = 'h2';
        d.nodes.get('a')!.revisionUid = 'r2';
        d.emit(EVENT.NodeUpdated, 'a');
        const r = await detectChanges(d, state);
        expect(r.changes.map((c) => `${c.action} ${c.from}->${c.path} (${c.reason})`)).toEqual(['moved a.pdf->b.pdf (content changed too)']);
    });

    test('the event log explains every event, including those outside the folder', async () => {
        const d = new FakeDrive();
        d.add({ uid: 'a', parentUid: 'W', name: 'a.pdf', type: 'file', size: 5, sha1: 'h1', revisionUid: 'r1' });
        const state = await started(d);
        d.add({ uid: 'x', parentUid: 'other', name: 'x.txt', type: 'file' });
        d.emit(EVENT.NodeCreated, 'x');
        d.emit(EVENT.NodeUpdated, 'a');
        const r = await detectChanges(d, state);
        expect(r.events.map((e) => `${e.type} ${e.nodeUid}: ${e.outcome}`)).toEqual([
            'node_created x: elsewhere on the volume (skipped, no lookup)',
            'node_updated a: inside: a.pdf (no visible change)',
        ]);
    });
});


describe('cheap filter on the clear fields of events (0.4)', () => {
    test('activity elsewhere on the volume costs no lookup at all', async () => {
        const d = new FakeDrive();
        d.add({ uid: 'a', parentUid: 'W', name: 'a.pdf', type: 'file' });
        const state = await started(d);
        for (let i = 0; i < 20; i++) {
            d.add({ uid: `x${i}`, parentUid: 'other', name: `x${i}.bin`, type: 'file' });
            d.emit(EVENT.NodeCreated, `x${i}`);
        }
        d.hierarchyCalls = 0;
        const r = await detectChanges(d, state);
        expect(r.eventsRead).toBe(20);
        expect(r.changes).toEqual([]);
        expect(d.hierarchyCalls).toBe(0);
        expect(r.notes).toContain('20 event(s) elsewhere on the volume skipped without lookup.');
    });

    test('a subfolder and its content created in the same batch are both seen', async () => {
        const d = new FakeDrive();
        const state = await started(d);
        d.add({ uid: 's', parentUid: 'W', name: 'new', type: 'folder' });
        d.emit(EVENT.NodeCreated, 's');
        d.add({ uid: 's1', parentUid: 's', name: 'in.txt', type: 'file' });
        d.emit(EVENT.NodeCreated, 's1');
        const r = await detectChanges(d, state);
        expect(r.changes.map((c) => `${c.action} ${c.path}`)).toEqual(['created new', 'created new/in.txt']);
    });

    for (const order of ['folder first', 'file first']) {
        test(`a known file moved into a new folder is one moved change (${order})`, async () => {
            const d = new FakeDrive();
            d.add({ uid: 'a', parentUid: 'W', name: 'a', type: 'folder' });
            d.add({ uid: 'f', parentUid: 'a', name: 'f.txt', type: 'file', size: 3, sha1: 'h' });
            const state = await started(d);
            d.add({ uid: 'b', parentUid: 'W', name: 'b', type: 'folder' });
            d.add({ uid: 'c', parentUid: 'b', name: 'c', type: 'folder' });
            d.nodes.get('f')!.parentUid = 'c';
            const steps: [string, string][] = [
                [EVENT.NodeCreated, 'b'],
                [EVENT.NodeCreated, 'c'],
                [EVENT.NodeUpdated, 'f'],
            ];
            for (const [t, u] of order === 'folder first' ? steps : [...steps].reverse()) {
                d.emit(t, u);
            }
            const r = await detectChanges(d, state);
            expect(r.changes.map((c) => `${c.action} ${c.from ?? ''}>${c.path}`).sort()).toEqual([
                'created >b',
                'created >b/c',
                'moved a/f.txt>b/c/f.txt',
            ]);
            expect(r.newState!.known.f.path).toBe('b/c/f.txt');
        });

        test(`a folder with children moved into a new folder is one moved change (${order})`, async () => {
            const d = new FakeDrive();
            d.add({ uid: 'k', parentUid: 'W', name: 'dir', type: 'folder' });
            d.add({ uid: 'k1', parentUid: 'k', name: 'in.txt', type: 'file' });
            d.add({ uid: 'k2', parentUid: 'k', name: 'sub', type: 'folder' });
            d.add({ uid: 'k3', parentUid: 'k2', name: 'deep.txt', type: 'file' });
            const state = await started(d);
            d.add({ uid: 'n', parentUid: 'W', name: 'new', type: 'folder' });
            d.nodes.get('k')!.parentUid = 'n';
            const steps: [string, string][] = [
                [EVENT.NodeCreated, 'n'],
                [EVENT.NodeUpdated, 'k'],
            ];
            for (const [t, u] of order === 'folder first' ? steps : [...steps].reverse()) {
                d.emit(t, u);
            }
            const r = await detectChanges(d, state);
            expect(r.changes.map((c) => `${c.action} ${c.from ?? ''}>${c.path}`).sort()).toEqual(['created >new', 'moved dir>new/dir']);
            expect(Object.values(r.newState!.known).map((e) => e.path).sort()).toEqual([
                'new',
                'new/dir',
                'new/dir/in.txt',
                'new/dir/sub',
                'new/dir/sub/deep.txt',
            ]);
        });
    }

    test('a file moved into a new folder with new content says so', async () => {
        const d = new FakeDrive();
        d.add({ uid: 'f', parentUid: 'W', name: 'f.txt', type: 'file', size: 3, sha1: 'h1', revisionUid: 'r1' });
        const state = await started(d);
        d.add({ uid: 'n', parentUid: 'W', name: 'new', type: 'folder' });
        d.emit(EVENT.NodeCreated, 'n');
        Object.assign(d.nodes.get('f')!, { parentUid: 'n', sha1: 'h2', revisionUid: 'r2' });
        d.emit(EVENT.NodeUpdated, 'f');
        const r = await detectChanges(d, state);
        expect(r.changes.map((c) => `${c.action} ${c.from ?? ''}>${c.path} (${c.reason})`).sort()).toEqual([
            'created >new (undefined)',
            'moved f.txt>new/f.txt (content changed too)',
        ]);
    });

    test('really new files in a new folder stay created', async () => {
        const d = new FakeDrive();
        const state = await started(d);
        d.add({ uid: 'n', parentUid: 'W', name: 'new', type: 'folder' });
        d.emit(EVENT.NodeCreated, 'n');
        d.add({ uid: 'n1', parentUid: 'n', name: 'x.txt', type: 'file' });
        d.emit(EVENT.NodeCreated, 'n1');
        const r = await detectChanges(d, state);
        expect(r.changes.map((c) => c.action)).toEqual(['created', 'created']);
    });

    test('a file arriving in a known deep subfolder is seen', async () => {
        const d = new FakeDrive();
        d.add({ uid: 's', parentUid: 'W', name: 'sub', type: 'folder' });
        d.add({ uid: 't', parentUid: 's', name: 'deep', type: 'folder' });
        const state = await started(d);
        d.add({ uid: 'f', parentUid: 'other', name: 'f.txt', type: 'file' });
        d.nodes.get('f')!.parentUid = 't';
        d.emit(EVENT.NodeUpdated, 'f');
        const r = await detectChanges(d, state);
        expect(r.changes.map((c) => `${c.action} ${c.path}`)).toEqual(['created sub/deep/f.txt']);
    });

    test('a known file deleted without a parent in the event is still seen', async () => {
        const d = new FakeDrive();
        d.add({ uid: 'a', parentUid: 'W', name: 'a.pdf', type: 'file' });
        const state = await started(d);
        d.nodes.delete('a');
        d.emit(EVENT.NodeDeleted, 'a');
        const r = await detectChanges(d, state);
        expect(r.changes.map((c) => `${c.action} ${c.path} (${c.reason})`)).toEqual(['removed a.pdf (deleted)']);
    });
});
