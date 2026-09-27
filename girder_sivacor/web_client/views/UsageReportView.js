import { errorMessage, formatHours, formatTimestamp } from '../utilities/format';
import UsageReportPageTemplate from '../templates/usageReportPage.pug';
import '../stylesheets/usageReport.styl';

const View = girder.views.View;
const { restRequest, cancelRestRequests } = girder.rest;

/**
 * Who has spent the compute allocation, cumulatively.
 *
 * The permanent sibling of the scratch-volume quota page, and the contrast is
 * the point: that one is derived from job documents on every call and so
 * reaches back exactly sivacor.retention_days, which answers "who is spending
 * the storage grant right now" and is useless for a quota. These counters live
 * on the user document, start at an account's first accrued run and are never
 * rewritten.
 *
 * Read-only, deliberately. There is no quota to enforce yet (09 stops at
 * measurement), and nothing here can be edited into a different past.
 */
const UsageReportView = View.extend({
    events: {
        'click .g-sivacor-refresh'(event) {
            event.preventDefault();
            this.fetch();
        }
    },

    initialize() {
        cancelRestRequests('fetch');
        this.report = null;
        this.error = null;
        this.loading = true;
        this.render();
        this.fetch();
    },

    fetch() {
        this.loading = true;
        this.error = null;
        this.render();
        restRequest({ url: 'sivacor/usage', error: null }).then((report) => {
            this.report = report;
            this.loading = false;
            this.render();
            return null;
        }).catch((resp) => {
            this.error = errorMessage(resp, 'Could not load resource usage.');
            this.loading = false;
            this.render();
        });
    },

    render() {
        const report = this.report;
        this.$el.html(UsageReportPageTemplate({
            report: report,
            error: this.error,
            loading: this.loading,
            // The total the server reports includes the house row; an operator
            // comparing "what did researchers cost" against it needs the other
            // half named rather than inferred from a subtraction.
            chargedSuHours: report
                ? report.total_su_hours - report.house.su_hours
                : 0,
            // The share, not just the figure. On the deployment this was first
            // read against, one instance reaped for a missing heartbeat was
            // 98.6% of every SU spent -- which the two numbers side by side
            // state and a reader scanning tiles still has to divide to see.
            houseLabel: report && report.total_su_hours
                ? `House (${((report.house.su_hours / report.total_su_hours) * 100)
                    .toFixed(1)}% of all SU)`
                : 'House',
            // Templates get no globals; helpers are passed explicitly.
            formatHours: formatHours,
            formatTimestamp: formatTimestamp
        }));
        return this;
    }
});

export default UsageReportView;
