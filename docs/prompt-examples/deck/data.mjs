// Deck content for one batch. The call text is pulled verbatim from the decision log by build.mjs; everything
// else is written here. One DECISIONS entry per decision; one SHOTS entry per screenshot.
// The entry below is a made-up example; replace it with your batch's decisions.
// A summary slide can be added as { id: 'summary', summary: true, title, what, ... } if the template needs one.
const A = (label, tradeoff, derived = true) => ({ label, tradeoff, derived });

// id: [path relative to the campaign folder (or shots/... relative to the deck folder), caption]
export const SHOTS = {
    exampleApp: ['research/decision-shots/app/S1D1-app-example.png', 'Branch <branch> at <sha>: the list page with the identifier masked.'],
};

export const DECISIONS = [
    {
        id: 'S1D1',
        title: 'Mask the identifier in list views',
        cases: ['<CASE-ID>'],
        what: 'The case asks for a searchable list of people. The prototype shows the full identifier; the roadmap says the browser sees only the last four digits.',
        reason: 'The roadmap wins over the prototype, and masking costs nothing for search because the last four digits are enough to tell people apart.',
        items: [
            {
                key: 'S1D1a',
                label: 'Show only the last four digits in every list and detail view',
                alts: [A('Show the full identifier to admins', 'Faster for support, but widens who can see it and needs an audit record'), A('Hide the identifier completely', 'Safest, but people with similar names become hard to tell apart')],
            },
        ],
        reg: [{ cite: '<REG> §<section>', quote: 'A closely paraphrased requirement that records holding personal data be protected from unauthorized disclosure.', src: 'official regulation text, read <date>' }],
        // or: reg: [], regNone: 'none: why this is not a regulatory question',
        proto: ['List screen, populated state: shows the full identifier (differs from the roadmap)'],
        factors: ['No new dependencies', 'Reopens the detail-view slide if the answer changes'],
        shots: ['exampleApp'],
        // or: shots: [], noShot: 'why there is no screenshot',
    },
];
