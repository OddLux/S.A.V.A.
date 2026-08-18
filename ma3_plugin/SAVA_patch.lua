--[[
  SAVA ArtNet patch helper - grandMA3 plugin
  ------------------------------------------------------------------
  Prompts for a universe, a starting Fixture ID and a starting Group
  number, then:

    * patches one single-channel dimmer per SAVA ArtNet function,
      at <universe>.<SAVA channel> (channels 1-18),
    * names each fixture after the function it drives
      (Play, Pause, Stop, Next track, Cue 1 ... Cue 8),
    * stores one Group per fixture (one fixture each), numbered
      sequentially from the given start, Mode = Additive.

  The channel layout mirrors config/artnet_config.ini in the SAVA repo.
  If you remap channels in SAVA, edit SAVA_CHANNELS below.

  ------------------------------------------------------------------
  METHOD

  This does NOT use AddFixtures(). It follows the command-line
  sequence MA3 itself uses in
    shared/resource/lib_plugins/systemtests/help/
      system_test_helping_functions_patch.lua
  which is the console's own verified end-to-end patching code:

    cd Root  ->  cd 'ShowData'.'Patch'     (enter the patch)
    cd <Stages.N.Fixtures address>
    Store <idx> /NC                        (create the fixture slot)
    Assign FixtureType '<name>' At <idx>
    Set <idx> property 'FID'   <fid>
    Set <idx> property 'Name'  '<name>'
    Set <idx> property 'Mode'  '<mode handle>'
    Set <idx> property 'Patch' <universe.address>
    cd Root

  <idx> is the fixture's POSITION in the stage's Fixtures list, not its
  Fixture ID. Entering the patch first is mandatory - outside it,
  Patch() resolves to LivePatch and the writes go nowhere.
--]]

-- ── SAVA channel map (name, DMX channel) ─────────────────────────
-- Order defines fixture-ID / group-number order.
local SAVA_CHANNELS = {
  { name = "Play",              channel = 1  },
  { name = "Pause",             channel = 2  },
  { name = "Stop",              channel = 3  },
  { name = "Next track",        channel = 4  },
  { name = "Prev track",        channel = 5  },
  { name = "Master volume",     channel = 6  },
  { name = "Seek",              channel = 7  },
  { name = "Track sel enable",  channel = 8  },
  { name = "Track select",      channel = 9  },
  { name = "Loop AB",           channel = 10 },
  { name = "Cue 1",             channel = 11 },
  { name = "Cue 2",             channel = 12 },
  { name = "Cue 3",             channel = 13 },
  { name = "Cue 4",             channel = 14 },
  { name = "Cue 5",             channel = 15 },
  { name = "Cue 6",             channel = 16 },
  { name = "Cue 7",             channel = 17 },
  { name = "Cue 8",             channel = 18 },
}

-- Fixture type names accepted as a single-channel dimmer, in order.
local DIMMER_FT_NAMES = { "Dimmer", "Generic Dimmer", "Dim" }

local STAGE_INDEX = 1            -- stage to patch into
local PREFIX      = ""           -- fixture/group name prefix

-- Value every stored group is parked at. Groups default to 100.
local GROUP_VALUE = 0

-- Prompt defaults. All three stay editable in the dialog.
local DEFAULT_UNIVERSE = 10
local DEFAULT_FID      = 1001
local DEFAULT_GROUP    = 10


-- ── helpers ──────────────────────────────────────────────────────

local function log(msg)
  Printf("[SAVA] %s", tostring(msg))
end

--- Cmd wrapper. MA3 returns "OK" (uppercase) on success.
--- Returns ok(boolean), raw result.
local function run(fmt, ...)
  local command = select("#", ...) > 0 and string.format(fmt, ...) or fmt
  local res     = Cmd(command)
  local text    = string.upper(tostring(res))
  return text == "OK", res, command
end

--- Same, but log and return false on failure.
local function runChecked(fmt, ...)
  local ok, res, command = run(fmt, ...)
  if not ok then
    log(("command failed: %s  ->  %s"):format(command, tostring(res)))
  end
  return ok
end

local function toInt(str, fallback)
  local num = tonumber(tostring(str or ""):match("^%s*(%d+)%s*$"))
  if num == nil then return fallback end
  return math.floor(num)
end

--- Ask for universe / start FID / start group in one dialog.
local function askUser()
  local res = MessageBox({
    title    = "SAVA ArtNet patch",
    message  = "Patch " .. #SAVA_CHANNELS ..
               " single-channel dimmers and store one group each.",
    commands = {
      { value = 1, name = "Patch"  },
      { value = 0, name = "Cancel" },
    },
    inputs = {
      { name = "Universe",           value = tostring(DEFAULT_UNIVERSE),
        whiteFilter = "0123456789", maxTextLength = 3 },
      { name = "Start Fixture ID",   value = tostring(DEFAULT_FID),
        whiteFilter = "0123456789", maxTextLength = 6 },
      { name = "Start Group number", value = tostring(DEFAULT_GROUP),
        whiteFilter = "0123456789", maxTextLength = 6 },
    },
  })

  if type(res) ~= "table" or res.success == false or res.result ~= 1 then
    return nil
  end

  local inp = res.inputs or {}
  return {
    universe = toInt(inp["Universe"],           DEFAULT_UNIVERSE),
    fid      = toInt(inp["Start Fixture ID"],   DEFAULT_FID),
    group    = toInt(inp["Start Group number"], DEFAULT_GROUP),
  }
end

--- Enter the patch. Mirrors SystemTest.UI:EnterPatch().
--- Without this, Patch() is LivePatch and nothing can be written.
local function enterPatch()
  if Patch() == Root().ShowData.LivePatch then
    run("cd Root")
    local ok, res = run("cd 'ShowData'.'Patch'")
    if not ok then
      log("entering patch failed: " .. tostring(res))
      return false
    end
  else
    run("cd Root 'ShowData'.'Patch'")
  end
  return true
end

local function leavePatch()
  run("cd Root")
end

--- The stage's Fixtures collection handle.
local function getFixturesObj()
  local ok, obj = pcall(function()
    local stage = Patch().Stages[STAGE_INDEX]
    return stage and stage["Fixtures"]
  end)
  if ok then return obj end
  return nil
end

--- Find a single-channel dimmer fixture type, importing one if needed.
--- Returns fixtureType handle, name -- or nil.
local function findDimmerType()
  local types = Patch().FixtureTypes

  for _, wanted in ipairs(DIMMER_FT_NAMES) do
    local ftype = types:Find(wanted)
    if ftype ~= nil and IsObjectValid(ftype) then
      return ftype, tostring(ftype.Name)
    end
  end

  -- Substring pass over everything present.
  for _, ftype in ipairs(types) do
    local fname = tostring(ftype.Name or "")
    if string.find(string.lower(fname), "dim", 1, true) then
      return ftype, fname
    end
  end

  -- Nothing: import the generic dimmer from the MA2 library.
  log("no dimmer fixture type in show - importing generic@dimmer")
  pcall(function()
    local lib = GetPath(Enums.PathType.GrandMA2Library) .. GetPathSeparator()
    Patch().FixtureTypes:Acquire():Import(lib, "generic@dimmer.pxml")
  end)

  for _, wanted in ipairs(DIMMER_FT_NAMES) do
    local ftype = Patch().FixtureTypes:Find(wanted)
    if ftype ~= nil and IsObjectValid(ftype) then
      return ftype, tostring(ftype.Name)
    end
  end

  log("could not find or import a dimmer fixture type. Present types:")
  for _, ftype in ipairs(Patch().FixtureTypes) do
    log("   - " .. tostring(ftype.Name))
  end
  return nil, nil
end

--- The single-channel DMX mode of a fixture type, as a handle string
--- suitable for Set ... property 'Mode'.
local function findModeString(ftype)
  local mode = ftype:FindRecursive("Mode 0", "DMXMode")
  if not IsObjectValid(mode) then
    mode = ftype.DMXModes and ftype.DMXModes:Ptr(1)
  end
  if mode == nil or not IsObjectValid(mode) then return nil, nil end
  return tostring(HandleToStr(mode)), tostring(mode.Name)
end

--- Set a group's master mode to Additive (Enums.GroupMasterMode).
local function setGroupAdditive(groupNo)
  if runChecked('Set Group %d Property "Mode" "Additive"', groupNo) then
    return true
  end
  local grp = DataPool().Groups:Ptr(groupNo)
  if grp ~= nil and pcall(function() grp.Mode = "Additive" end) then
    return true
  end
  log(("Group %d left at default mode - set Additive by hand"):format(groupNo))
  return false
end

--- Park a group at GROUP_VALUE. Groups store at 100 by default.
--- The property name is not documented, so probe the candidates once on
--- the first group and reuse whichever MA3 accepts for the rest.
--- Park a group's master at `value`. The group master is not a settable
--- object property - it is driven by the FaderMaster command.
local function setGroupValue(groupNo, value)
  return runChecked("FaderMaster Group %d At %s", groupNo, tostring(value))
end


-- ── main ─────────────────────────────────────────────────────────

return function()
  local opts = askUser()
  if opts == nil then
    log("cancelled")
    return
  end

  if opts.universe < 1 or opts.universe > 256 then
    log("universe must be 1-256, got " .. tostring(opts.universe))
    return
  end
  if opts.fid < 1 or opts.group < 1 then
    log("start Fixture ID and Group number must be >= 1")
    return
  end

  if not enterPatch() then return end

  local ftype, ftName = findDimmerType()
  if ftype == nil then
    leavePatch()
    return
  end

  local modeStr, modeName = findModeString(ftype)
  if modeStr == nil then
    log("fixture type '" .. tostring(ftName) .. "' has no usable DMX mode")
    leavePatch()
    return
  end
  log(("using fixture type '%s', mode '%s'"):format(ftName, modeName))

  local fixtures = getFixturesObj()
  if fixtures == nil then
    log(("stage %d has no Fixtures collection - nothing patched"):format(STAGE_INDEX))
    leavePatch()
    return
  end

  -- Move the command line onto the Fixtures collection.
  local addr = fixtures:ToAddr()
  if not runChecked("cd %s", addr) then
    log("could not cd to " .. tostring(addr))
    leavePatch()
    return
  end
  log("patching into " .. tostring(addr))

  -- New fixtures are appended after whatever is already there.
  local base = fixtures:Count()

  local patched = {}
  for offset, entry in ipairs(SAVA_CHANNELS) do
    local idx     = base + offset
    local fid     = opts.fid + offset - 1
    local fxName  = PREFIX .. entry.name
    local address = string.format("%d.%d", opts.universe, entry.channel)

    local ok = runChecked("Store %d /NC", idx)
    ok = ok and runChecked("Assign FixtureType '%s' At %d", ftName, idx)
    ok = ok and runChecked("Set %d property 'FID' %d", idx, fid)
    ok = ok and runChecked("Set %d property 'Name' '%s'", idx, fxName)
    ok = ok and runChecked("Set %d property 'Mode' '%s'", idx, modeStr)
    ok = ok and runChecked("Set %d property 'Patch' %s", idx, address)

    if ok then
      patched[#patched + 1] = { fid = fid, name = fxName, address = address }
      log(("patched [%d] FID %-4d %-20s @ %s"):format(idx, fid, fxName, address))
    else
      log(("FAILED at position %d (%s) - skipping its group"):format(idx, fxName))
    end
  end

  leavePatch()

  if #patched == 0 then
    log("nothing was patched - no groups stored")
    return
  end

  -- Groups: one fixture each, Additive.
  for offset, item in ipairs(patched) do
    local groupNo = opts.group + offset - 1
    run("ClearAll")
    runChecked("Fixture %d", item.fid)
    runChecked('Store Group %d /NC /O', groupNo)
    runChecked('Label Group %d "%s"', groupNo, item.name)
    setGroupAdditive(groupNo)
    setGroupValue(groupNo, GROUP_VALUE)
    log(("stored Group %-4d %-20s (FID %d)"):format(groupNo, item.name, item.fid))
  end
  run("ClearAll")

  log(("done - %d fixtures (FID %d-%d), %d groups (%d-%d), universe %d")
    :format(#patched,
            opts.fid, opts.fid + #patched - 1,
            #patched, opts.group, opts.group + #patched - 1,
            opts.universe))
end
