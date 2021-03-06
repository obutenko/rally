# Copyright 2014: Mirantis Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import collections
import hashlib
import json

import six

from rally.common import objects
from rally.common.plugin import plugin
from rally.common import version
from rally.task.processing import charts
from rally.ui import utils as ui_utils


def _process_scenario(data, pos):
    main_area = charts.MainStackedAreaChart(data["info"])
    main_hist = charts.MainHistogramChart(data["info"])
    main_stat = charts.MainStatsTable(data["info"])
    load_profile = charts.LoadProfileChart(data["info"])
    atomic_pie = charts.AtomicAvgChart(data["info"])
    atomic_area = charts.AtomicStackedAreaChart(data["info"])
    atomic_hist = charts.AtomicHistogramChart(data["info"])

    errors = []
    output_errors = []
    additive_output_charts = []
    complete_output = []
    for idx, itr in enumerate(data["iterations"], 1):
        if itr["error"]:
            typ, msg, trace = itr["error"]
            errors.append({"iteration": idx,
                           "type": typ, "message": msg, "traceback": trace})

        for i, additive in enumerate(itr["output"]["additive"]):
            try:
                additive_output_charts[i].add_iteration(additive["data"])
            except IndexError:
                chart_cls = plugin.Plugin.get(additive["chart_plugin"])
                chart = chart_cls(
                    data["info"], title=additive["title"],
                    description=additive.get("description", ""),
                    label=additive.get("label", ""),
                    axis_label=additive.get("axis_label",
                                            "Iteration sequence number"))
                chart.add_iteration(additive["data"])
                additive_output_charts.append(chart)

        complete_charts = []
        for complete in itr["output"]["complete"]:
            complete_chart = dict(complete)
            chart_cls = plugin.Plugin.get(complete_chart.pop("chart_plugin"))
            complete_chart["widget"] = chart_cls.widget
            complete_charts.append(complete_chart)
        complete_output.append(complete_charts)

        for chart in (main_area, main_hist, main_stat, load_profile,
                      atomic_pie, atomic_area, atomic_hist):
            chart.add_iteration(itr)

    kw = data["key"]["kw"]
    cls, method = data["key"]["name"].split(".")
    additive_output = [chart.render() for chart in additive_output_charts]
    iterations_count = data["info"]["iterations_count"]
    return {
        "cls": cls,
        "met": method,
        "pos": str(pos),
        "name": method + (pos and " [%d]" % (pos + 1) or ""),
        "runner": kw["runner"]["type"],
        "config": json.dumps({data["key"]["name"]: [kw]}, indent=2),
        "iterations": {
            "iter": main_area.render(),
            "pie": [("success", (data["info"]["iterations_count"]
                                 - len(errors))),
                    ("errors", len(errors))],
            "histogram": main_hist.render()},
        "load_profile": load_profile.render(),
        "atomic": {"histogram": atomic_hist.render(),
                   "iter": atomic_area.render(),
                   "pie": atomic_pie.render()},
        "table": main_stat.render(),
        "additive_output": additive_output,
        "complete_output": complete_output,
        "output_errors": output_errors,
        "errors": errors,
        "load_duration": data["info"]["load_duration"],
        "full_duration": data["info"]["full_duration"],
        "sla": data["sla"],
        "sla_success": all([s["success"] for s in data["sla"]]),
        "iterations_count": iterations_count,
    }


def _process_tasks(tasks_results):
    tasks = []
    source_dict = collections.defaultdict(list)
    position = collections.defaultdict(lambda: -1)

    for scenario in tasks_results:
        name = scenario["key"]["name"]
        position[name] += 1
        source_dict[name].append(scenario["key"]["kw"])
        tasks.append(_process_scenario(scenario, position[name]))

    source = json.dumps(source_dict, indent=2, sort_keys=True)
    return source, sorted(tasks, key=lambda r: (r["cls"], r["met"],
                                                int(r["pos"])))


def _extend_results(results):
    """Transform tasks results into extended format.

    This is a temporary workaround adapter that allows
    working with task results using new schema, until
    database refactoring actually comes.

    :param results: tasks results list in old format
    :returns: tasks results list in new format
    """
    extended_results = []
    for result in results:
        generic = {"id": None,
                   "task_uuid": None,
                   "key": result["key"],
                   "data": {"sla": result["sla"],
                            "hooks": result["hooks"],
                            "raw": result["result"],
                            "full_duration": result["full_duration"],
                            "load_duration": result["load_duration"]},
                   "created_at": None,
                   "updated_at": None}
        extended_results.extend(
            objects.Task.extend_results([generic]))
    return extended_results


def plot(tasks_results, include_libs=False):
    extended_results = _extend_results(tasks_results)
    template = ui_utils.get_template("task/report.html")
    source, data = _process_tasks(extended_results)
    return template.render(version=version.version_string(),
                           source=json.dumps(source),
                           data=json.dumps(data),
                           include_libs=include_libs)


def trends(tasks_results):
    trends = Trends()
    for i, scenario in enumerate(_extend_results(tasks_results), 1):
        trends.add_result(scenario)
    template = ui_utils.get_template("task/trends.html")
    return template.render(version=version.version_string(),
                           data=json.dumps(trends.get_data()))


class Trends(object):
    """Process workloads results and make trends data.

    Group workloads results by their input configuration,
    calculate statistics for these groups and prepare it
    for displaying in trends HTML report.
    """

    def __init__(self):
        self._data = {}

    def _to_str(self, obj):
        """Convert object into string."""
        if obj is None:
            return "None"
        elif isinstance(obj, six.string_types + (int, float)):
            return str(obj).strip()
        elif isinstance(obj, (list, tuple)):
            return ",".join(sorted([self._to_str(v) for v in obj]))
        elif isinstance(obj, dict):
            return "|".join(sorted([":".join([self._to_str(k),
                                              self._to_str(v)])
                                    for k, v in obj.items()]))
        raise TypeError("Unexpected type %(type)r of object %(obj)r"
                        % {"obj": obj, "type": type(obj)})

    def _make_hash(self, obj):
        return hashlib.md5(self._to_str(obj).encode("utf8")).hexdigest()

    def add_result(self, result):
        key = self._make_hash(result["key"]["kw"])
        if key not in self._data:
            self._data[key] = {
                "actions": {},
                "sla_failures": 0,
                "name": result["key"]["name"],
                "config": json.dumps(result["key"]["kw"], indent=2)}

        for sla in result["sla"]:
            self._data[key]["sla_failures"] += not sla["success"]

        stat = {row[0]: dict(zip(result["info"]["stat"]["cols"], row))
                for row in result["info"]["stat"]["rows"]}
        ts = int(result["info"]["tstamp_start"] * 1000)

        for action in stat:
            # NOTE(amaretskiy): some atomic actions can be missed due to
            #   failures. We can ignore that because we use NVD3 lineChart()
            #   for displaying trends, which is safe for missed points
            if action not in self._data[key]["actions"]:
                self._data[key]["actions"][action] = {
                    "durations": {"min": [], "median": [], "90%ile": [],
                                  "95%ile": [], "max": [], "avg": []},
                    "success": []}

            try:
                success = float(stat[action]["Success"].rstrip("%"))
            except ValueError:
                # Got "n/a" for some reason
                success = 0

            self._data[key]["actions"][action]["success"].append(
                (ts, success))

            for tgt, src in (("min", "Min (sec)"),
                             ("median", "Median (sec)"),
                             ("90%ile", "90%ile (sec)"),
                             ("95%ile", "95%ile (sec)"),
                             ("max", "Max (sec)"),
                             ("avg", "Avg (sec)")):

                self._data[key]["actions"][action]["durations"][tgt].append(
                    (ts, stat[action][src]))

    def get_data(self):
        trends = []

        for wload in self._data.values():
            trend = {"stat": {},
                     "name": wload["name"],
                     "cls": wload["name"].split(".")[0],
                     "met": wload["name"].split(".")[1],
                     "sla_failures": wload["sla_failures"],
                     "config": wload["config"],
                     "actions": []}

            for action, data in wload["actions"].items():
                action_durs = [(k, sorted(v))
                               for k, v in data["durations"].items()]
                if action == "total":
                    trend.update(
                        {"length": len(data["success"]),
                         "durations": action_durs,
                         "success": [("success", sorted(data["success"]))]})
                else:
                    trend["actions"].append(
                        {"name": action,
                         "durations": action_durs,
                         "success": [("success", sorted(data["success"]))]})

            for stat, comp in (("min", charts.streaming.MinComputation()),
                               ("max", charts.streaming.MaxComputation()),
                               ("avg", charts.streaming.MeanComputation())):
                for k, v in trend["durations"]:
                    for i in v:
                        if isinstance(i[1], (float,) + six.integer_types):
                            comp.add(i[1])
                trend["stat"][stat] = comp.result()

            trends.append(trend)

        return sorted(trends, key=lambda i: i["name"])
