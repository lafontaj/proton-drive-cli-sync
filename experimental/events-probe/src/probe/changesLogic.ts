/**
 * Change detection for one watched Drive folder, driven by the Drive event
 * stream (`iterateEvents`) instead of walking the remote tree.
 *
 * Part of an experimental, read-only probe for proton-drive-cli-sync. It never
 * writes anything to Proton Drive: it reads events and node metadata, and keeps
 * its own small state file (bookmark + what it knows about the folder).
 *
 * The logic is kept apart from the SDK so it can be tested with a fake.
 */

export const STATE_VERSION = 1;

export type KnownEntry = {
    path: string;
    type: string;
    size?: number;
    modificationTime?: string;
    /** Active revision of a file; absent in states written by version 0.1. */
    revisionUid?: string;
    /** SHA-1 of the content, as claimed by the uploader. */
    sha1?: string;
};

export type ProbeState = {
    version: number;
    folderUid: string;
    folderPath: string;
    scopeId: string;
    lastEventId: string;
    known: Record<string, KnownEntry>;
};

/**
 * - created: new in the folder (or arrived from elsewhere)
 * - modified: content changed
 * - revised: new version uploaded, same content (nothing to fetch)
 * - moved: renamed or moved inside the folder
 * - removed: trashed, deleted or moved out of the folder
 */
export type ChangeAction = 'created' | 'modified' | 'revised' | 'moved' | 'removed';

export type Change = {
    action: ChangeAction;
    type: string;
    path: string;
    from?: string;
    size?: number;
    modificationTime?: string;
    sha1?: string;
    reason?: string;
};

/** One event as read, with what the probe made of it (for --events). */
export type EventLogEntry = {
    eventId: string;
    type: string;
    nodeUid?: string;
    outcome: string;
};

/** Minimal view of a node, as the probe needs it. */
export type ProbeNode = {
    uid: string;
    parentUid?: string;
    name: string;
    type: string;
    trashed: boolean;
    size?: number;
    modificationTime?: string;
    revisionUid?: string;
    sha1?: string;
};

/** Minimal view of a Drive event. */
export type ProbeEvent = {
    type: string;
    eventId: string;
    nodeUid?: string;
    /** Parent of the node after the change, in clear in the event. */
    parentNodeUid?: string;
    isTrashed?: boolean;
};

/** What the logic needs from the SDK. */
export interface ProbeSdk {
    /** Root → node, or null when the node no longer exists. */
    getHierarchy(nodeUid: string): Promise<ProbeNode[] | null>;
    /** Direct children of a folder, trashed ones included. */
    listChildren(folderUid: string): Promise<ProbeNode[]>;
    /** Events after `lastEventId`; without it, one FastForward with the current id. */
    iterateEvents(scopeId: string, lastEventId?: string): AsyncIterable<ProbeEvent>;
}

export type RunResult = {
    initialized: boolean;
    eventsRead: number;
    changes: Change[];
    notes: string[];
    events: EventLogEntry[];
    newState: ProbeState | null;
};

export const EVENT = {
    NodeCreated: 'node_created',
    NodeUpdated: 'node_updated',
    NodeDeleted: 'node_deleted',
    TreeRefresh: 'tree_refresh',
    TreeRemove: 'tree_remove',
    FastForward: 'fast_forward',
} as const;

/** Lists the whole subtree under a folder, skipping trashed items. Paths are relative. */
export async function listSubtree(
    sdk: ProbeSdk,
    folderUid: string,
    prefix: string = '',
): Promise<Record<string, KnownEntry>> {
    const out: Record<string, KnownEntry> = {};
    for (const child of await sdk.listChildren(folderUid)) {
        if (child.trashed) {
            continue;
        }
        const path = prefix ? `${prefix}/${child.name}` : child.name;
        out[child.uid] = entryOf(child, path);
        if (child.type === 'folder') {
            Object.assign(out, await listSubtree(sdk, child.uid, path));
        }
    }
    return out;
}

function entryOf(node: ProbeNode, path: string): KnownEntry {
    const entry: KnownEntry = {
        path,
        type: node.type,
        size: node.size,
        modificationTime: node.modificationTime,
    };
    if (node.revisionUid) {
        entry.revisionUid = node.revisionUid;
    }
    if (node.sha1) {
        entry.sha1 = node.sha1;
    }
    return entry;
}

/**
 * How the content of a file compares between two observations.
 *
 * The SHA-1 decides when both sides have one. Without it, a size or date
 * difference means a change, and so does a new revision: a backup must not
 * skip a version it cannot prove identical.
 */
export function compareContent(before: KnownEntry, now: KnownEntry): 'same' | 'changed' | 'revised' {
    if (now.type === 'folder') {
        return 'same';
    }
    const newRevision = !!before.revisionUid && !!now.revisionUid && before.revisionUid !== now.revisionUid;
    if (before.sha1 && now.sha1) {
        if (before.sha1 !== now.sha1) {
            return 'changed';
        }
        return newRevision ? 'revised' : 'same';
    }
    if (before.size !== now.size || before.modificationTime !== now.modificationTime) {
        return 'changed';
    }
    return newRevision ? 'changed' : 'same';
}

type Located =
    | { where: 'inside'; node: ProbeNode; path: string }
    | { where: 'trashed' }
    | { where: 'outside' }
    | { where: 'gone' };

/** Where a node is now, relative to the watched folder. */
async function locate(sdk: ProbeSdk, folderUid: string, nodeUid: string): Promise<Located> {
    const chain = await sdk.getHierarchy(nodeUid);
    if (!chain || chain.length === 0) {
        return { where: 'gone' };
    }
    const index = chain.findIndex((n) => n.uid === folderUid);
    if (index < 0) {
        return chain.some((n) => n.trashed) ? { where: 'trashed' } : { where: 'outside' };
    }
    const below = chain.slice(index + 1);
    if (below.length === 0) {
        // The watched folder itself; handled by the caller.
        return { where: 'outside' };
    }
    if (below.some((n) => n.trashed)) {
        return { where: 'trashed' };
    }
    return { where: 'inside', node: below[below.length - 1]!, path: below.map((n) => n.name).join('/') };
}

/** First run: bookmark first, then list, so nothing between the two is lost. */
export async function initialize(
    sdk: ProbeSdk,
    folder: { uid: string; path: string; scopeId: string },
): Promise<RunResult> {
    let lastEventId: string | undefined;
    for await (const event of sdk.iterateEvents(folder.scopeId)) {
        lastEventId = event.eventId;
    }
    if (!lastEventId) {
        throw new Error('Proton did not return a starting event id');
    }
    const known = await listSubtree(sdk, folder.uid);
    return {
        initialized: true,
        eventsRead: 0,
        changes: [],
        notes: [`Starting point set: ${Object.keys(known).length} item(s) known in ${folder.path}.`],
        events: [],
        newState: {
            version: STATE_VERSION,
            folderUid: folder.uid,
            folderPath: folder.path,
            scopeId: folder.scopeId,
            lastEventId,
            known,
        },
    };
}

/** Later runs: read the events since the bookmark and turn them into changes. */
export async function detectChanges(sdk: ProbeSdk, state: ProbeState): Promise<RunResult> {
    const notes: string[] = [];
    const touched: string[] = [];
    const events: EventLogEntry[] = [];
    let eventsRead = 0;
    let lastEventId = state.lastEventId;
    let refresh = false;

    // Cheap filter, from the clear fields of the event: only nodes the probe
    // knows in the folder, or nodes whose parent is the folder, one of its
    // known subfolders, or a node that became relevant earlier in this batch
    // (a subfolder created together with its content). Everything else is
    // activity elsewhere on the volume: skipped without any lookup, so without
    // fetching or decrypting anything.
    const parents = new Set<string>([state.folderUid]);
    for (const [uid, entry] of Object.entries(state.known)) {
        if (entry.type === 'folder') {
            parents.add(uid);
        }
    }
    const isRelevant = (event: ProbeEvent): boolean =>
        !!event.nodeUid &&
        (event.nodeUid === state.folderUid ||
            event.nodeUid in state.known ||
            (!!event.parentNodeUid && parents.has(event.parentNodeUid)));
    let skipped = 0;

    for await (const event of sdk.iterateEvents(state.scopeId, state.lastEventId)) {
        events.push({ eventId: event.eventId, type: event.type, nodeUid: event.nodeUid, outcome: '' });
        if (event.type === EVENT.TreeRemove) {
            notes.push('The volume of the watched folder is no longer accessible. State reset; next run starts over.');
            events.at(-1)!.outcome = 'volume no longer accessible';
            return { initialized: false, eventsRead, changes: [], notes, events, newState: null };
        }
        eventsRead += event.type === EVENT.FastForward ? 0 : 1;
        lastEventId = event.eventId;
        if (event.type === EVENT.TreeRefresh) {
            refresh = true;
            continue;
        }
        if (!event.nodeUid) {
            continue;
        }
        if (!isRelevant(event)) {
            if (!touched.includes(event.nodeUid)) {
                events.at(-1)!.outcome = 'elsewhere on the volume (skipped, no lookup)';
                skipped++;
            }
            continue;
        }
        parents.add(event.nodeUid);
        if (!touched.includes(event.nodeUid)) {
            touched.push(event.nodeUid);
        }
    }
    if (skipped > 0) {
        notes.push(`${skipped} event(s) elsewhere on the volume skipped without lookup.`);
    }

    const outcomes = new Map<string, string>();
    const describe = (): RunResult['events'] =>
        events.map((e) => ({
            ...e,
            outcome:
                e.outcome ||
                (e.type === EVENT.FastForward
                    ? 'bookmark moved forward, nothing happened'
                    : e.type === EVENT.TreeRefresh
                      ? 'full refresh requested'
                      : (e.nodeUid && outcomes.get(e.nodeUid)) || 'no node'),
        }));

    // The watched folder itself was trashed, moved or deleted.
    if (touched.includes(state.folderUid)) {
        const chain = await sdk.getHierarchy(state.folderUid);
        const self = chain?.[chain.length - 1];
        if (!self || self.trashed || chain.some((n) => n.trashed)) {
            notes.push(`The watched folder ${state.folderPath} was trashed or deleted. State reset; next run starts over.`);
            outcomes.set(state.folderUid, 'the watched folder itself: trashed or deleted');
            return { initialized: false, eventsRead, changes: [], notes, events: describe(), newState: null };
        }
        outcomes.set(state.folderUid, 'the watched folder itself: still in place');
    }

    const known: Record<string, KnownEntry> = structuredClone(state.known);
    let changes: Change[];

    if (refresh) {
        notes.push('Proton asked for a full refresh of this volume: the folder was listed again.');
        const now = await listSubtree(sdk, state.folderUid);
        changes = diffListings(known, now);
        return finish(state, lastEventId, now, changes, eventsRead, notes, describe());
    }

    changes = [];
    for (const uid of touched) {
        if (uid === state.folderUid) {
            continue;
        }
        const before = known[uid];
        const now = await locate(sdk, state.folderUid, uid);

        if (now.where === 'inside') {
            const entry = entryOf(now.node, now.path);
            if (!before) {
                changes.push(change('created', entry));
                outcomes.set(uid, `inside: ${entry.path} (created)`);
                known[uid] = entry;
                if (entry.type === 'folder') {
                    const inner = await listSubtree(sdk, uid, entry.path);
                    const innerMoves: { entry: KnownEntry; from: string; content: ReturnType<typeof compareContent> }[] = [];
                    for (const [innerUid, innerEntry] of Object.entries(inner)) {
                        const old = known[innerUid];
                        if (!old) {
                            changes.push(change('created', innerEntry));
                        } else if (old.path !== innerEntry.path) {
                            // Already known at another path: it was moved into this new folder.
                            innerMoves.push({ entry: innerEntry, from: old.path, content: compareContent(old, innerEntry) });
                        }
                        known[innerUid] = innerEntry;
                    }
                    // A moved folder gives one change, not one per child, whatever the listing order.
                    for (const m of innerMoves) {
                        const carried = innerMoves.some(
                            (p) =>
                                p !== m &&
                                p.entry.type === 'folder' &&
                                m.from.startsWith(`${p.from}/`) &&
                                m.entry.path === p.entry.path + m.from.slice(p.from.length),
                        );
                        if (carried) {
                            if (m.content === 'changed') {
                                changes.push(change('modified', m.entry));
                            }
                            continue;
                        }
                        const moved: Change = { ...change('moved', m.entry), from: m.from };
                        if (m.content === 'changed') {
                            moved.reason = 'content changed too';
                        }
                        changes.push(moved);
                    }
                }
                continue;
            }
            const content = compareContent(before, entry);
            if (before.path !== entry.path) {
                const moved: Change = { ...change('moved', entry), from: before.path };
                if (content === 'changed') {
                    moved.reason = 'content changed too';
                }
                changes.push(moved);
                outcomes.set(uid, `inside: ${entry.path} (moved)`);
                movePrefix(known, before.path, entry.path);
            } else if (content === 'changed') {
                changes.push(change('modified', entry));
                outcomes.set(uid, `inside: ${entry.path} (modified)`);
            } else if (content === 'revised') {
                changes.push({ ...change('revised', entry), reason: 'new version, identical content' });
                outcomes.set(uid, `inside: ${entry.path} (new version, identical content)`);
            } else {
                outcomes.set(uid, `inside: ${entry.path} (no visible change)`);
            }
            known[uid] = entry;
            continue;
        }

        if (before) {
            let reason = { trashed: 'trashed', outside: 'moved out of the folder', gone: 'deleted' }[now.where];
            const inner = removePrefix(known, before.path);
            if (inner > 0) {
                reason += `, with the ${inner} item(s) it contained`;
            }
            changes.push({ ...change('removed', before), reason });
            outcomes.set(uid, `was ${before.path}: ${reason}`);
            delete known[uid];
        } else {
            outcomes.set(uid, `not in the folder (${now.where})`);
        }
    }

    return finish(state, lastEventId, known, changes, eventsRead, notes, describe());
}

function finish(
    state: ProbeState,
    lastEventId: string,
    known: Record<string, KnownEntry>,
    changes: Change[],
    eventsRead: number,
    notes: string[],
    events: EventLogEntry[],
): RunResult {
    return {
        initialized: false,
        eventsRead,
        changes,
        notes,
        events,
        newState: { ...state, lastEventId, known },
    };
}

function change(action: ChangeAction, entry: KnownEntry): Change {
    const c: Change = {
        action,
        type: entry.type,
        path: entry.path,
        size: entry.size,
        modificationTime: entry.modificationTime,
    };
    if (entry.sha1) {
        c.sha1 = entry.sha1;
    }
    return c;
}

/** Compares two complete listings (used after a full refresh). */
export function diffListings(before: Record<string, KnownEntry>, now: Record<string, KnownEntry>): Change[] {
    const changes: Change[] = [];
    for (const [uid, entry] of Object.entries(now)) {
        const old = before[uid];
        if (!old) {
            changes.push(change('created', entry));
            continue;
        }
        const content = compareContent(old, entry);
        if (old.path !== entry.path) {
            changes.push({ ...change('moved', entry), from: old.path });
        } else if (content === 'changed') {
            changes.push(change('modified', entry));
        } else if (content === 'revised') {
            changes.push({ ...change('revised', entry), reason: 'new version, identical content' });
        }
    }
    for (const [uid, entry] of Object.entries(before)) {
        if (!now[uid]) {
            changes.push({ ...change('removed', entry), reason: 'no longer in the folder' });
        }
    }
    return changes;
}

function movePrefix(known: Record<string, KnownEntry>, from: string, to: string): void {
    for (const entry of Object.values(known)) {
        if (entry.path.startsWith(`${from}/`)) {
            entry.path = to + entry.path.slice(from.length);
        }
    }
}

function removePrefix(known: Record<string, KnownEntry>, path: string): number {
    let count = 0;
    for (const [uid, entry] of Object.entries(known)) {
        if (entry.path.startsWith(`${path}/`)) {
            delete known[uid];
            count++;
        }
    }
    return count;
}
